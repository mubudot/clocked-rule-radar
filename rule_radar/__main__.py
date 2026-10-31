"""Command line: python -m rule_radar {analyze,check,watch,serve}."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .clocked import ClockedError
from .config import Settings, load_dotenv
from .pipeline import InputError, analyze, check_claims, exit_code
from .report_text import render

EXIT_ERROR = 2


def _profile(arg: str | None) -> dict:
    if not arg:
        return {}
    p = Path(arg)
    return json.loads(p.read_text("utf-8") if p.is_file() else arg)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="rule_radar", description="Find what changed in a contest's rules.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="compare two saved versions of a rules page")
    a.add_argument("old"), a.add_argument("new")
    a.add_argument("--profile", help="entrant situation as JSON or a JSON file")
    a.add_argument("--json", action="store_true", help="print the full report as JSON")

    c = sub.add_parser("check", help="verify claims about the rules against both versions")
    c.add_argument("old"), c.add_argument("new")
    c.add_argument("claims", nargs="+")

    w = sub.add_parser("watch", help="snapshot a rules URL and report changes since the last snapshot")
    w.add_argument("url")
    w.add_argument("--profile")
    w.add_argument("--store", default=".radar/snapshots")

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)

    args = ap.parse_args(argv)
    settings = Settings.from_env()

    if args.cmd == "serve":
        from .server import serve
        serve(settings, args.host, args.port)
        return 0

    client = settings.clocked()
    try:
        if args.cmd == "analyze":
            old, new = Path(args.old).read_text("utf-8"), Path(args.new).read_text("utf-8")
            rep = analyze(old, new, client=client, llm=settings.model(), profile=_profile(args.profile))
            print(json.dumps(rep, ensure_ascii=False, indent=1) if args.json else render(rep))
            return exit_code(rep)
        if args.cmd == "check":
            old, new = Path(args.old).read_text("utf-8"), Path(args.new).read_text("utf-8")
            out = check_claims(old, new, args.claims, client=client)
            for row in out["claims"]:
                ev = (row.get("evidence") or {}).get("quote") or ""
                print(f"[{row['status'].upper():8}] {row['claim']}" + (f"\n           evidence: {ev}" if ev else ""))
            return 0
        if args.cmd == "watch":
            from .snapshots import watch
            return watch(args.url, Path(args.store), client=client, llm=settings.model(),
                         profile=_profile(args.profile))
    except (InputError, ClockedError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
