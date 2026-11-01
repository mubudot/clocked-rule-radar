// Small DOM helpers. All text goes in through textContent, never innerHTML.

export function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "dataset") Object.assign(n.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

// Render `text` with the given [start, end) ranges wrapped in <mark>.
// Offsets are Unicode code points (Python string indices, as Clocked returns them), so the text is
// indexed through Array.from() rather than UTF-16 String.slice().
export function highlighted(text, ranges, cls = "") {
  const cps = Array.from(text);
  const frag = document.createDocumentFragment();
  const rs = (ranges || [])
    .filter((r) => Number.isInteger(r[0]) && Number.isInteger(r[1]) && r[0] >= 0 && r[0] < r[1] && r[1] <= cps.length)
    .sort((a, b) => a[0] - b[0]);
  let pos = 0;
  let first = null;
  for (const [s, e] of rs) {
    if (s < pos) continue;
    frag.append(document.createTextNode(cps.slice(pos, s).join("")));
    const m = el("mark", { class: cls }, cps.slice(s, e).join(""));
    if (!first) first = m;
    frag.append(m);
    pos = e;
  }
  frag.append(document.createTextNode(cps.slice(pos).join("")));
  return { frag, first };
}

// Word-level diff (LCS over tokens) -> [{op: "=", "-", "+", text}]
export function wordDiff(a, b) {
  const ta = a.split(/(\s+)/);
  const tb = b.split(/(\s+)/);
  if (ta.length * tb.length > 250000) return [{ op: "-", text: a }, { op: "+", text: b }];
  const n = ta.length, m = tb.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = ta[i] === tb[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out = [];
  const push = (op, text) => {
    const last = out[out.length - 1];
    if (last && last.op === op) last.text += text; else out.push({ op, text });
  };
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (ta[i] === tb[j]) { push("=", ta[i]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { push("-", ta[i]); i++; }
    else { push("+", tb[j]); j++; }
  }
  while (i < n) push("-", ta[i++]);
  while (j < m) push("+", tb[j++]);
  return out;
}

export const pct = (x) => (typeof x === "number" ? `${Math.round(x * 100)}%` : "—");

// Replay matches exact rules and profile inputs. Rumours are recorded separately, so any
// subset/order of the sample claims is supported (each claim makes its own request).
export function replayDifferences(sample, current) {
  const keys = new Set([...Object.keys(sample?.profile || {}), ...Object.keys(current.profile)]);
  return {
    rules: !sample || current.old !== sample.old || current.new !== sample.new,
    profile: !sample || [...keys].some((key) => current.profile[key] !== sample.profile[key]),
    claims: current.claims.some((claim) => !(sample?.claims || []).includes(claim)),
  };
}
