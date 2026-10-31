import { el, clear, highlighted, wordDiff, pct } from "./dom.js";

const $ = (id) => document.getElementById(id);
const state = { report: null, version: "new", filter: "all", sample: null, selected: null, sources: null };

const PROFILE_FIELDS = [
  ["country_of_residence", "Country of residence", "text", "e.g. Taiwan"],
  ["team_size", "Team size", "number", "1"],
  ["project_started", "Project started", "date", ""],
  ["repository", "Repository", "text", "e.g. private GitHub repo"],
  ["demo_video_length", "Demo video length", "text", "e.g. 4 minutes"],
];
const LABELS = { act: "Act on this", read_section: "Read the section", low_impact: "Low impact",
  not_your_case: "Not your case" };
const MATERIALITY = {
  material: "rule change · Compare found a conflict", wording: "wording only · same meaning",
  unconfirmed: "edited · Compare did not confirm — read it", new: "new clause", removed: "removed clause",
  moved: "moved · same content elsewhere",
};
const STATUS_BADGE = { verified: "✓", check: "?", rejected: "✗", not_a_claim: "–", unverified: "·" };
const STAGES = [["diff", "Local diff"], ["compare", "Compare"], ["extract", "Extract"], ["rerank", "Rerank"],
  ["brief", "Brief"], ["verify", "Verify"], ["repair", "Repair"]];

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

function showError(msg) {
  const box = $("errorBox");
  box.textContent = msg || "";
  box.hidden = !msg;
}

/* ---------- setup ---------- */

function buildProfile() {
  const grid = $("profileGrid");
  for (const [key, label, type, ph] of PROFILE_FIELDS) {
    grid.append(el("label", {}, label, el("input", { type, id: `p_${key}`, placeholder: ph, min: type === "number" ? 1 : null })));
  }
}

function readProfile() {
  const p = {};
  for (const [key, , type] of PROFILE_FIELDS) {
    const v = $(`p_${key}`).value.trim();
    if (v) p[key] = type === "number" ? Number(v) : v;
  }
  return p;
}

function fillProfile(p = {}) {
  for (const [key] of PROFILE_FIELDS) $(`p_${key}`).value = p[key] ?? "";
}

async function loadConfig() {
  const cfg = await api("/api/config");
  const chips = clear($("modeChips"));
  const live = cfg.mode !== "replay";
  chips.append(el("span", { class: `chip ${live ? "live" : "replay"}` }, el("span", { class: "dot" }),
    live ? `Clocked: live${cfg.mode === "record" ? " (recording)" : ""}` : "Clocked: replay of recorded responses"));
  // In replay mode the bundled sample's brief comes from the recorded model output, so say so;
  // after a scan the chip shows who actually wrote the brief (brief.source).
  const writer = !live ? "Brief: recorded model output (bundled sample)"
    : cfg.llm_provider === "offline" ? "Brief: template (no model)" : `Brief: ${cfg.llm_provider} · ${cfg.llm_model}`;
  chips.append(el("span", { class: "chip", id: "briefChip" }, el("span", { class: "dot" }), writer));
  if (!live) $("runHint").textContent = "Replay mode: the bundled sample runs on recorded Clocked responses. Set CLOCKED_API_KEY for your own text.";
}

async function loadSamples() {
  const list = await api("/api/samples");
  const sel = $("sampleSelect");
  for (const s of list) sel.append(el("option", { value: s.name }, s.title));
  sel.addEventListener("change", () => pickSample(sel.value));
  if (list.length) { sel.value = list[0].name; await pickSample(list[0].name); }
}

async function pickSample(name) {
  if (!name) { state.sample = null; renderClaimChips([]); return; }
  const s = await api(`/api/samples/${encodeURIComponent(name)}`);
  state.sample = s;
  $("oldText").value = s.old;
  $("newText").value = s.new;
  fillProfile(s.profile);
  renderClaimChips(s.claims || []);
}

/* ---------- run ---------- */

async function run() {
  const btn = $("runBtn");
  showError("");
  btn.disabled = true;
  clear(btn).append(el("span", { class: "spinner" }), "Scanning…");
  try {
    const rep = await api("/api/analyze", { old: $("oldText").value, new: $("newText").value, profile: readProfile() });
    state.report = rep;
    state.selected = null;
    state.sources = rep.versions;
    const chip = $("briefChip");
    if (chip && rep.brief && rep.brief.source) clear(chip).append(el("span", { class: "dot" }), `Brief: written by ${rep.brief.source}`);
    render();
    $("results").hidden = false;
    $("verdict").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    showError(`Scan failed: ${e.message}`);
  } finally {
    btn.disabled = false;
    clear(btn).append("Scan for changes");
  }
}

async function checkClaims() {
  const claims = $("claimText").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const list = clear($("checkList"));
  if (!claims.length) return;
  const btn = $("checkBtn");
  btn.disabled = true;
  try {
    const sources = { old: $("oldText").value, new: $("newText").value };  // evidence offsets refer to these
    const out = await api("/api/check", { ...sources, claims });
    for (const row of out.claims) list.append(claimItem(row, row.claim, true, sources));
  } catch (e) {
    list.append(el("li", { class: "empty" }, `Check failed: ${e.message}`));
  } finally {
    btn.disabled = false;
  }
}

function renderClaimChips(claims) {
  const row = clear($("claimChips"));
  for (const c of claims) {
    row.append(el("button", { type: "button", onclick: () => {
      const t = $("claimText");
      t.value = (t.value.trim() ? t.value.trim() + "\n" : "") + c;
    } }, `“${c}”`));
  }
}

/* ---------- render ---------- */

function render() {
  const r = state.report;
  renderVerdict(r);
  renderBrief(r.brief);
  renderFilters();
  renderChanges();
  renderFacts(r.fields || []);
  renderStages(r);
  showSource(state.version);
}

function renderVerdict(r) {
  const v = clear($("verdict"));
  const a = r.assessment || {};
  const an = r.action_needed;
  const c = r.counts || {};
  const act = r.changes.filter((x) => x.rank && x.rank.label === "act").length;
  const notMine = r.changes.filter((x) => x.rank && x.rank.label === "not_your_case").length;
  const unknown = a.action === "unknown";
  const yes = a.action === "yes";
  v.classList.toggle("calm", a.action === "no");
  v.classList.toggle("unknown", unknown);
  let says;
  const gaps = [...(a.failed_stages || []).map((k) => `${k} failed`), ...(a.partial_stages || []).map((k) => `${k} ran only in part`)];
  if (unknown) says = `Could not rank every change for you (${gaps.join(", ")}). Read every change below yourself.`;
  else if (act) says = `${act} ${act > 1 ? "changes affect" : "change affects"} what you must do before the deadline.`;
  else says = yes ? "Rerank says you need to act; see the changes below." : "No change needs action from you.";
  v.append(
    el("div", { class: "big" }, el("small", {}, "Action needed"), unknown ? "Unknown" : yes ? "Yes" : "No",
      !unknown && an && typeof an.probability === "number" ? el("span", { class: "score" }, ` ${pct(an.probability)}`) : null),
    el("div", { class: "says" }, says,
      !unknown && notMine ? ` ${notMine} ${notMine > 1 ? "changes apply" : "change applies"} to other entrants only.` : "",
      a.complete === false && !unknown ? el("div", { class: "notverified" }, `Incomplete: ${gaps.join(", ") || "Verify did not fully run"} — see Pipeline.`) : null,
      el("div", { class: "hint" }, `${r.usage.calls} Clocked calls · ${r.usage.credits} credits · ${(r.elapsed_ms / 1000).toFixed(1)} s`
        + (r.usage.replayed ? " · replayed recordings" : ""))),
    el("div", { class: "counts" },
      count(c.material || 0, "rule changes"), count((c.new || 0) + (c.unconfirmed || 0), "new / unconfirmed"),
      count(c.removed || 0, "removed"), count(c.wording || 0, "wording only")),
  );
}

const count = (n, label) => el("div", { class: "count" }, el("b", {}, n), el("span", {}, label));

function renderBrief(b) {
  const list = clear($("briefList"));
  $("briefSource").textContent = "";
  $("briefSource").append(
    b.verified ? `Every line checked by Verify${typeof b.grounded === "number" ? ` · grounded ${pct(b.grounded)}` : ""}`
      : el("span", { class: "notverified" }, b.partial ? "PARTLY VERIFIED — some lines got no judgment" : "NOT VERIFIED — Verify did not run"),
    ` · written by ${b.source}`);
  if (!b.lines.length) list.append(el("li", { class: "empty" }, "Nothing in the brief survived verification."));
  for (const ln of b.lines) list.append(claimItem(ln, ln.text, false));
  const rej = clear($("rejectedList"));
  $("rejectedBox").hidden = !b.rejected.length;
  for (const ln of b.rejected) rej.append(claimItem(ln, ln.claim, true));
}

function claimItem(row, text, showVerdict, sources = null) {
  const ev = row.evidence;
  const why = [];
  if (row.repaired_from) why.push(el("span", { class: "repaired" }, "rewritten"), el("span", { class: "why" }, `Verify rejected: “${row.repaired_from}”`));
  if (showVerdict || row.status === "rejected" || row.status === "not_a_claim") {
    const word = { verified: "Supported", check: "Weakly supported", rejected: row.verdict === "not_stated" ? "Not stated in either version" : "Contradicted by the rules", not_a_claim: "Not a factual claim — nothing to check", unverified: row.error ? `Not verified (${row.error})` : "Not verified" }[row.status];
    why.push(el("span", { class: "why" }, word, ev && ev.quote ? [": ", el("q", {}, ev.quote), ` (${ev.document === "old" ? "before" : "now"})`] : ""));
  }
  const li = el("li", { tabindex: 0 },
    el("span", { class: `badge b-${row.status}`, title: row.status }, STATUS_BADGE[row.status] || "·"),
    el("div", {}, el("span", { class: "txt" }, text), ...why),
    el("span", { class: "score", title: "Verify: probability the sources support this line" }, typeof row.supported === "number" ? pct(row.supported) : ""));
  const go = () => {
    select(li);
    if (ev && Number.isInteger(ev.start)) showSource(ev.document === "old" ? "old" : "new", [[ev.start, ev.end]], row.status === "rejected" ? "" : "ok",
      `Verify evidence for: “${text}”`, sources);
    else {
      const ch = (row.change_ids || []).map((id) => state.report.changes.find((c) => c.id === id)).find(Boolean);
      if (ch) focusChange(ch); else showSource(state.version, [], "", "Verify returned no passage for this line.");
    }
  };
  li.addEventListener("click", go);
  li.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  return li;
}

function renderFilters() {
  const f = clear($("filters"));
  const opts = [["all", "All"], ["act", "Act on this"], ["not_your_case", "Not your case"], ["wording", "Wording only"]];
  for (const [k, label] of opts) {
    f.append(el("button", { type: "button", "aria-pressed": state.filter === k ? "true" : "false",
      onclick: () => { state.filter = k; renderFilters(); renderChanges(); } }, label));
  }
}

function visible(c) {
  const label = c.rank && c.rank.label;
  if (state.filter === "wording") return c.materiality === "wording";
  if (c.materiality === "wording") return false;
  if (state.filter === "all") return true;
  return label === state.filter;
}

function renderChanges() {
  const box = clear($("changeList"));
  const items = state.report.changes.filter(visible);
  if (!items.length) box.append(el("p", { class: "empty" }, "No changes in this view."));
  for (const c of items) {
    const label = (c.rank && c.rank.label) || "unranked";
    const rel = c.rank && typeof c.rank.relevance === "number" ? c.rank.relevance : null;
    const node = el("div", { class: `change l-${label}`, tabindex: 0, dataset: { id: c.id } },
      el("div", { class: "change-top" },
        el("span", { class: `pill ${label}` }, LABELS[label] || "Unranked"),
        el("span", { class: "sec" }, c.section || "Rules"),
        el("span", { class: "meta" }, MATERIALITY[c.materiality] || c.materiality,
          c.compare && c.compare.kind ? ` (${String(c.compare.kind).replace(/_/g, " ")}, ${pct(c.compare.confidence)})` : "",
          c.shortened_for_rerank ? " · ranked on a shortened passage" : ""),
        rel !== null ? el("span", { class: "rel", title: "Rerank relevance for your situation" },
          el("span", { class: "bar" }, el("i", { style: null })), pct(rel)) : null),
      diffBlock(c));
    if (rel !== null) node.querySelector(".bar i").style.width = `${Math.max(3, Math.round(rel * 100))}%`;
    const go = () => focusChange(c);
    node.addEventListener("click", go);
    node.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    box.append(node);
  }
}

function diffBlock(c) {
  const d = el("div", { class: "diff" });
  if (c.status === "edited") {
    const parts = wordDiff(c.old.text, c.new.text);
    d.append(el("div", { class: "line" }, el("span", { class: "tagline" }, "was"),
      el("span", {}, parts.filter((p) => p.op !== "+").map((p) => (p.op === "-" ? el("del", {}, p.text) : p.text)))));
    d.append(el("div", { class: "line" }, el("span", { class: "tagline" }, "now"),
      el("span", {}, parts.filter((p) => p.op !== "-").map((p) => (p.op === "+" ? el("ins", {}, p.text) : p.text)))));
  } else if (c.status === "added") {
    d.append(el("div", { class: "line" }, el("span", { class: "tagline" }, "new"), el("span", {}, el("ins", {}, c.new.text))));
  } else {
    d.append(el("div", { class: "line" }, el("span", { class: "tagline" }, "gone"), el("span", {}, el("del", {}, c.old.text))));
  }
  return d;
}

function focusChange(c) {
  const node = document.querySelector(`.change[data-id="${CSS.escape(c.id)}"]`);
  if (node) select(node);
  const side = c.new ? "new" : "old";
  const cl = c[side];
  showSource(side, [[cl.start, cl.end]], c.materiality === "wording" ? "ok" : "", `${c.section}: ${MATERIALITY[c.materiality] || ""}`);
}

function renderFacts(rows) {
  const tb = clear($("factsTable").tBodies[0]);
  if (!rows.length) { tb.append(el("tr", {}, el("td", { colspan: 2, class: "empty" }, "Extract did not run."))); return; }
  const order = { changed: 0, added: 1, removed: 2, same: 3, not_stated: 4 };
  for (const f of [...rows].sort((a, b) => order[a.status] - order[b.status])) {
    const val = (side) => {
      const r = f[side];
      if (!r || r.value === null || (Array.isArray(r.value) && !r.value.length)) return el("span", { class: "ns" }, "not stated");
      return Array.isArray(r.value) ? r.value.join("; ") : String(r.value);
    };
    const cell = el("td", { class: "v", tabindex: 0 });
    if (f.status === "changed" || f.status === "removed") cell.append(el("span", { class: "was" }, val("old")));
    cell.append(f.status === "removed" ? el("span", { class: "ns" }, "no longer stated") : val("new"));
    if (f.uncertain) cell.append(el("span", { class: "lowconf" }, " · low confidence, check"));
    const go = () => {
      const side = f.new && f.new.spans && f.new.spans.length ? "new" : "old";
      showSource(side, (f[side] && f[side].spans) || [], "", `Extract: ${f.label}`);
    };
    cell.addEventListener("click", go);
    tb.append(el("tr", { class: f.status }, el("td", {}, f.label), cell));
  }
}

function renderStages(r) {
  const ol = clear($("stageList"));
  let ms = 0;
  for (const [key, label] of STAGES) {
    const s = r.stages[key];
    if (!s) continue;
    if (s.usage) ms += s.usage.model_ms || 0;
    ol.append(el("li", { class: `stage ${s.status}` }, el("b", {}, label), el("span", { class: "st" }, s.status),
      el("span", { class: "n" }, s.status === "failed" ? `${s.error} → ${s.fallback}` : s.note || ""),
      s.usage ? el("span", { class: "n" }, `${s.usage.credits} credits · ${s.usage.calls} call${s.usage.calls !== 1 ? "s" : ""}`
        + (s.usage.failed_calls ? ` · ${s.usage.failed_calls} failed` : "")
        + (s.usage.attempts > s.usage.calls ? ` · ${s.usage.attempts - s.usage.calls} retr${s.usage.attempts - s.usage.calls > 1 ? "ies" : "y"}` : "")) : null));
  }
  $("traceSum").textContent = `${r.usage.credits} credits · ${Math.round(ms)} ms model time`;
}

function select(node) {
  document.querySelectorAll(".sel").forEach((n) => n.classList.remove("sel"));
  node.classList.add("sel");
}

function showSource(version, ranges = [], cls = "", note = "", sources = null) {
  state.version = version;
  if (sources) state.sources = sources;          // a rumour check ran on these exact texts
  else if (ranges.length || !state.sources) state.sources = state.report ? state.report.versions : null;
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.v === version)));
  const text = state.sources ? state.sources[version] : "";
  const pre = clear($("sourceText"));
  const { frag, first } = highlighted(text, ranges, cls);
  pre.append(frag);
  $("sourceNote").textContent = note || "Click any line, fact or change to see where it comes from.";
  pre.scrollTop = first ? Math.max(0, first.offsetTop - pre.clientHeight / 3) : 0;
}

/* ---------- boot ---------- */

buildProfile();
$("runBtn").addEventListener("click", run);
$("checkBtn").addEventListener("click", checkClaims);
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showSource(b.dataset.v)));
Promise.all([loadConfig(), loadSamples()]).catch((e) => showError(`Could not load the app: ${e.message}`));
