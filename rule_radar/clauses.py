"""Split a rules document into clauses and align two versions.

Everything here is local and deterministic. It gives every later stage a shared
vocabulary: a clause has a stable id, a section and exact character offsets in its
own version, so any span Clocked returns can be mapped back to the clauses it covers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

ABBREVIATIONS = {"a.m.", "p.m.", "e.g.", "i.e.", "u.s.", "u.k.", "etc.", "no.", "nos.", "mr.", "ms.",
                 "mrs.", "dr.", "st.", "inc.", "ltd.", "co.", "corp.", "vs.", "approx.", "art.",
                 "sec.", "fig.", "jan.", "feb.", "mar.", "apr.", "jun.", "jul.", "aug.", "sep.",
                 "sept.", "oct.", "nov.", "dec."}
TIME_ZONE_WORDS = {"pacific", "eastern", "central", "mountain", "et", "pt", "ct", "mt", "edt", "est", "pdt", "pst",
                   "cdt", "cst", "utc", "gmt", "cet", "cest", "bst", "ist", "jst", "kst", "aest", "aoe",
                   "anywhere", "local", "time", "on", "in", "at"}
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_NUMBERED_HEADING = re.compile(r"^\s*(\d+(?:\.\d+)*\.?|[IVX]+\.|Section\s+\d+[.:]?)\s+([A-Z][^.!?]{0,80})$")
# Words that make a numbered line a rule ("1. Teams must be public"), not a section title ("1. Teams").
_RULE_WORDS = re.compile(
    r"\b(must|may|shall|should|will|can|cannot|can't|won't|need|needs|is|are|was|were|be|been|being|"
    r"has|have|had|do|does|not|no|only|never|always|allowed|required|requires|require|accepted|"
    r"eligible|ineligible|close|closes|open|opens|runs|run|receive|receives|submit|submitted|"
    r"start|starts|end|ends|apply|applies|get|gets|win|wins|pay|paid|pays)\b", re.I)
_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\(?[a-z0-9]{1,3}[.)])\s+")
_SECTION_NUM = re.compile(r"^\s*(?:section\s+)?(?:\d+(?:\.\d+)*\.?|[ivx]+\.)\s*", re.I)


@dataclass(frozen=True)
class Clause:
    id: str            # e.g. "new:4.2" = 2nd clause of the 4th section of the new version
    version: str       # "old" | "new"
    section: str       # section heading as written ("" before the first heading)
    text: str          # clause text, exactly doc[start:end]
    start: int
    end: int

    def contains(self, start: int, end: int) -> bool:
        return self.start <= start and end <= self.end

    def overlaps(self, start: int, end: int) -> int:
        """Number of characters shared with [start, end)."""
        return max(0, min(self.end, end) - max(self.start, start))

    def as_dict(self) -> dict:
        return {"id": self.id, "version": self.version, "section": self.section,
                "text": self.text, "start": self.start, "end": self.end}


def _sentence_spans(text: str, base: int) -> list[tuple[int, int]]:
    """Sentence boundaries inside one paragraph or list item (closing quotes stay with their sentence)."""
    spans, start = [], 0
    for m in re.finditer(r"[.!?;][\"')\]\u201d\u2019]*(?=\s+[\"'(\[\u201c\u2018]?([A-Za-z0-9$]+))", text):
        end = m.end()
        nxt = m.group(1)
        if not nxt[0].isupper() and not nxt[0].isdigit() and nxt[0] != "$":
            continue
        words = text[start:end].split()
        token = words[-1].lower().strip("\"')]\u201d\u2019([\u201c\u2018") if words else ""
        if token in ("a.m.", "p.m."):
            if nxt.lower() in TIME_ZONE_WORDS:
                continue  # "11:59 p.m. Pacific Time" is one sentence; "5 p.m. Teams ..." is two
        elif token in ABBREVIATIONS or re.fullmatch(r"[a-z]\.", token):
            continue
        if re.fullmatch(r"\d+(?:\.\d+)*\.", token) and len(words) == 1:
            continue  # list numbering such as "3. " at the start of a segment
        if m.group(0)[0] == ";" and len(text[start:end].strip()) < 25:
            continue  # keep short list fragments together
        spans.append((start, end))
        start = end
    spans.append((start, len(text)))
    out = []
    for s, e in spans:
        seg = text[s:e]
        lead = len(seg) - len(seg.lstrip())
        trail = len(seg.rstrip())
        if trail > lead:
            out.append((base + s + lead, base + s + trail))
    return out


def _is_numbered_heading(stripped: str, next_line: str) -> bool:
    """'3. Teams' is a heading; '3. Teams must be public' in a numbered list is a rule."""
    if not _NUMBERED_HEADING.match(stripped) or len(stripped) >= 90 or stripped[-1] in ".;:!?,":
        return False
    if len(stripped.split()) > 7:
        return False
    title = _NUMBERED_HEADING.match(stripped).group(2)
    if _RULE_WORDS.search(title) or re.search(r"\d", title):
        return False  # reads as a rule or carries a value -> keep it as a clause
    nxt = next_line.strip()
    # a heading is followed by body text or a bullet list, not by the next numbered item
    return bool(nxt) and not re.match(r"\s*\(?\d+(?:\.\d+)*[.)]?\s", next_line)


def split_clauses(doc: str, version: str) -> list[Clause]:
    """Headings start sections; each sentence of a paragraph or list item is a clause."""
    clauses: list[Clause] = []
    section, sec_idx, clause_idx = "", 0, 0
    pos = 0
    para_start: int | None = None

    def flush(upto: int) -> None:
        nonlocal para_start, clause_idx
        if para_start is None:
            return
        block = doc[para_start:upto]
        for s, e in _sentence_spans(block, para_start):
            clause_idx += 1
            clauses.append(Clause(f"{version}:{sec_idx}.{clause_idx}", version, section, doc[s:e], s, e))
        para_start = None

    lines = doc.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.strip()
        h = _HEADING.match(line.rstrip("\r\n"))
        next_line = next((ln for ln in lines[i + 1:] if ln.strip()), "")
        if h or (stripped and _is_numbered_heading(stripped, next_line)):
            flush(pos)
            section = (h.group(2) if h else stripped).strip()
            sec_idx += 1
            clause_idx = 0
        elif not stripped:
            flush(pos)
        elif _BULLET.match(line):
            flush(pos)
            para_start = pos + _BULLET.match(line).end()
        elif para_start is None:
            para_start = pos + (len(line) - len(line.lstrip()))
        pos += len(line)
    flush(pos)
    return clauses


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def section_key(heading: str) -> str:
    return norm(_SECTION_NUM.sub("", heading))


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b), autojunk=False).ratio()


@dataclass
class Change:
    """One aligned pair of clauses (either side may be missing)."""
    status: str                 # unchanged | edited | added | removed
    old: Clause | None
    new: Clause | None
    similarity: float = 0.0

    @property
    def id(self) -> str:
        return (self.new or self.old).id  # type: ignore[union-attr]

    @property
    def section(self) -> str:
        return (self.new or self.old).section  # type: ignore[union-attr]


def _pair_block(olds: list[Clause], news: list[Clause], min_sim: float) -> list[Change]:
    """Inside a replaced block, pair clauses greedily by similarity, keeping order."""
    out: list[Change] = []
    j = 0
    used_new: set[int] = set()
    for o in olds:
        best, best_sim = None, min_sim
        for k in range(j, len(news)):
            if k in used_new:
                continue
            s = similarity(o.text, news[k].text)
            if s > best_sim:
                best, best_sim = k, s
        if best is None:
            out.append(Change("removed", o, None))
            continue
        for k in range(j, best):  # new clauses skipped over are additions
            if k not in used_new:
                out.append(Change("added", None, news[k]))
                used_new.add(k)
        out.append(Change("edited", o, news[best], best_sim))
        used_new.add(best)
        j = best + 1
    for k in range(len(news)):
        if k not in used_new:
            out.append(Change("added", None, news[k]))
    return out


def align(old: list[Clause], new: list[Clause], min_sim: float = 0.45) -> list[Change]:
    """Align by section (matched on heading text), then by sentence within a section."""
    def group(cs: list[Clause]) -> dict[str, list[Clause]]:
        g: dict[str, list[Clause]] = {}
        for c in cs:
            g.setdefault(section_key(c.section), []).append(c)
        return g

    og, ng = group(old), group(new)
    # Renamed sections: pair leftover headings that are similar enough.
    rename: dict[str, str] = {}
    left_old = [k for k in og if k not in ng]
    left_new = [k for k in ng if k not in og]
    for ko in left_old:
        cands = [(similarity(ko, kn), kn) for kn in left_new if kn not in rename.values()]
        if cands:
            s, kn = max(cands)
            if s >= 0.6:
                rename[ko] = kn

    changes: list[Change] = []
    emitted_new: set[str] = set()
    order = [section_key(c.section) for c in new]
    seen: list[str] = []
    for k in order:
        if k not in seen:
            seen.append(k)
    inv = {v: k for k, v in rename.items()}
    for kn in seen:
        ko = kn if kn in og else inv.get(kn)
        olds = og.get(ko, []) if ko is not None else []
        news = ng[kn]
        changes.extend(_align_section(olds, news, min_sim))
        emitted_new.add(kn)
        if ko is not None:
            og.pop(ko, None)
    for ko, olds in og.items():  # sections that disappeared entirely
        changes.extend(Change("removed", o, None) for o in olds)
    return changes


def _align_section(olds: list[Clause], news: list[Clause], min_sim: float) -> list[Change]:
    sm = SequenceMatcher(None, [norm(c.text) for c in olds], [norm(c.text) for c in news], autojunk=False)
    out: list[Change] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(Change("unchanged", o, n, 1.0) for o, n in zip(olds[i1:i2], news[j1:j2]))
        elif tag == "insert":
            out.extend(Change("added", None, n) for n in news[j1:j2])
        elif tag == "delete":
            out.extend(Change("removed", o, None) for o in olds[i1:i2])
        else:
            out.extend(_pair_block(olds[i1:i2], news[j1:j2], min_sim))
    return out
