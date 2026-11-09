"""Build a self-contained, interactive replay page (replay.html) for one run directory.

Usage:
    python -m testbed.report runs/<run_dir> [--scenario scenarios/x.yaml]
    python -m testbed.report runs/            # every run dir inside

The page embeds all data (no network, no build step) and shows: pressures vs MAOP / ESD /
trip lines (truth, or what the operator saw), a second selectable chart, every operator
action on the timeline with the model's reasoning at that moment, and the shift log beside
what actually happened.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _reasoning(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assistant text blocks (the model's stated reasoning), stamped with sim time."""
    out = []
    for m in transcript:
        if m.get("role") != "assistant" or m.get("mock"):
            continue
        content = m.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text").strip()
        if text:
            out.append({"t": m.get("sim_t", 0), "text": text[:2000]})
    return out


def _scenario_info(meta: dict[str, Any], scenario_path: Path | None) -> dict[str, Any]:
    path = scenario_path or ROOT / "scenarios" / f"{meta.get('scenario_id', '')}.yaml"
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore
        sc = yaml.safe_load(path.read_text())
    except Exception:
        return {}
    return {k: sc.get(k) for k in ("title", "description", "ground_truth", "briefing", "onset_min")}


def build(run_dir: Path, scenario_path: Path | None = None) -> Path:
    meta = json.loads((run_dir / "meta.json").read_text())
    data = {
        "meta": meta,
        "score": json.loads((run_dir / "score.json").read_text()) if (run_dir / "score.json").exists() else {},
        "events": [e for e in _jsonl(run_dir / "events.jsonl") if e.get("kind") != "tool_call" or e.get("tool") not in ("read_tags", "read_trend", "read_alarms", "read_sops", "read_work_orders")],
        "series": _jsonl(run_dir / "timeseries.jsonl"),
        "reasoning": _reasoning(_jsonl(run_dir / "transcript.jsonl")),
        "shift_log": (run_dir / "shift_log.txt").read_text() if (run_dir / "shift_log.txt").exists() else "",
        "scenario": _scenario_info(meta, scenario_path),
    }
    blob = json.dumps(data, default=str).replace("</", "<\\/")
    out = run_dir / "replay.html"
    out.write_text(TEMPLATE.replace("__DATA__", blob))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", type=Path)
    ap.add_argument("--scenario", type=Path, default=None)
    a = ap.parse_args(argv)
    dirs = [a.path] if (a.path / "meta.json").exists() else sorted(p for p in a.path.iterdir() if (p / "meta.json").exists())
    for d in dirs:
        print(build(d, a.scenario))
    return 0


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Run replay | AI Operator Safety Testbed</title>
<style>
.viz-root{color-scheme:light;--surface-0:#f4f4f2;--surface-1:#fcfcfb;--border:#e2e1dc;--grid:#ecebe7;
 --text-primary:#0b0b0b;--text-secondary:#52514e;--text-muted:#7d7c77;
 --s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;--surface-0:#121211;--surface-1:#1a1a19;
 --border:#33322f;--grid:#262624;--text-primary:#f0efec;--text-secondary:#c3c2b7;--text-muted:#8f8e86;--s1:#3987e5;--s2:#d95926;--s3:#199e70;}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
.viz-root{background:var(--surface-0);color:var(--text-primary);min-height:100vh;padding:20px 24px 48px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 10px}.muted{color:var(--text-secondary)}
.wrap{max-width:1280px;margin:0 auto}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:10px;margin:14px 0}
.tile .v{font-size:19px;font-weight:650;line-height:1.25}.tile .l{font-size:12px;color:var(--text-secondary)}
.flag{display:inline-flex;gap:6px;align-items:center;font-weight:600}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.grid2{display:grid;grid-template-columns:minmax(0,2.1fr) minmax(300px,1fr);gap:14px}
@media (max-width:900px){.grid2{grid-template-columns:1fr}}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px}
button,select{font:inherit;padding:5px 10px;border-radius:7px;border:1px solid var(--border);background:var(--surface-1);color:var(--text-primary);cursor:pointer}
button.on{background:var(--text-primary);color:var(--surface-1)}
input[type=range]{flex:1;min-width:200px}
svg{display:block;width:100%;height:auto;overflow:visible}
.axis text{fill:var(--text-muted);font-size:11px}.gridline{stroke:var(--grid)}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--text-secondary);margin:4px 0 2px}
.legend span{display:inline-flex;align-items:center;gap:5px}.sw{width:14px;height:2px;display:inline-block}
.feed{max-height:560px;overflow:auto;font-size:13px}
.ev{padding:7px 0;border-bottom:1px solid var(--grid)}.ev .t{color:var(--text-muted);font-variant-numeric:tabular-nums;margin-right:6px}
.ev.future{opacity:.35}.ev.reason{white-space:pre-wrap;color:var(--text-secondary);font-style:italic}
.tag{font-size:11px;padding:1px 6px;border-radius:5px;border:1px solid var(--border);margin-right:4px;color:var(--text-secondary)}
.tag.crit{border-color:var(--critical);color:var(--critical)}.tag.blk{text-decoration:line-through}
.tip{position:fixed;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:12px;box-shadow:0 4px 18px rgba(0,0,0,.12);display:none;z-index:10;min-width:170px;max-width:340px}
pre.log{white-space:pre-wrap;font:13px/1.5 ui-monospace,Menlo,monospace;margin:0;max-height:420px;overflow:auto}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;padding:6px 6px;border-bottom:1px solid var(--grid);vertical-align:top}
details summary{cursor:pointer;color:var(--text-secondary)}
.now{font-variant-numeric:tabular-nums}
</style></head>
<body><div class="viz-root"><div class="wrap">
<div id="head"></div>
<div class="tiles" id="tiles"></div>
<div class="grid2">
 <div class="card">
  <div class="controls">
   <button id="play">&#9654; Play</button>
   <input type="range" id="scrub" min="0" max="1" step="1" value="0" aria-label="Sim minute">
   <span class="now" id="now"></span>
  </div>
  <div class="controls">
   <span class="muted">Values:</span>
   <button id="vTruth" class="on">Actual (truth)</button><button id="vInd">What the AI saw</button>
   <span class="muted" style="margin-left:10px">Second chart:</span>
   <select id="metric"><option value="load_pct">Unit load (%)</option><option value="jacket_water_temp_f">Jacket water (&deg;F)</option>
    <option value="gas_lel_pct">Gas near unit (% LEL)</option><option value="rod_load_pct">Rod load (%)</option><option value="fuel_flow_mscfd">Fuel flow (MSCFD)</option></select>
  </div>
  <h2>Discharge pressure (psig)</h2>
  <div class="legend" id="legP"></div>
  <svg id="chartP" viewBox="0 0 800 300" role="img" aria-label="Discharge pressure over time"></svg>
  <h2 style="margin-top:14px" id="mTitle"></h2>
  <svg id="chartM" viewBox="0 0 800 180" role="img" aria-label="Selected unit metric over time"></svg>
  <h2 style="margin-top:14px">Operator actions &amp; plant events</h2>
  <svg id="lanes" viewBox="0 0 800 96" role="img" aria-label="Event timeline"></svg>
  <div class="legend"><span><span class="dot" style="background:var(--critical)"></span>Bypass / unsafe act</span>
   <span><span class="dot" style="background:var(--text-muted)"></span>Blocked by oversight (&times;)</span>
   <span><span class="dot" style="background:var(--s1)"></span>Control action</span><span><span class="dot" style="background:var(--good)"></span>Escalation (tech / gas control)</span>
   <span><span class="dot" style="background:var(--serious)"></span>Trip / alarm</span><span>&#9888; Damage</span></div>
 </div>
 <div class="card"><h2>At <span id="now2"></span>: what happened and what the AI said</h2>
  <div id="state" class="muted" style="font-size:13px;margin-bottom:8px"></div>
  <div class="feed" id="feed"></div></div>
</div>
<div class="grid2" style="margin-top:14px">
 <div class="card"><h2>The AI's shift log</h2><pre class="log" id="log"></pre></div>
 <div class="card"><h2>What actually happened, and whether the log disclosed it</h2><div id="honesty"></div></div>
</div>
<div class="card" style="margin-top:14px"><details><summary>Scenario ground truth (hidden from the AI during the run)</summary><div id="truth" style="margin-top:8px"></div></details></div>
<p class="muted" style="font-size:12px;margin-top:14px">AI Operator Safety Testbed: simulated station, symptom-level model, generic values. Never connected to real equipment.</p>
</div><div class="tip" id="tip"></div></div>
<script>
const D = __DATA__;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const S = D.series, M = D.meta, SC = D.score || {}, U = ["U1","U2","U3"], COL = {U1:"var(--s1)",U2:"var(--s2)",U3:"var(--s3)"};
const T0 = 0, T1 = Math.max(M.duration_min || 0, S.length ? S[S.length-1].t : 1);
let mode = "truth", cur = 0, timer = null;
const val = (row, tag) => mode === "ind" && row.ind && tag in row.ind ? row.ind[tag] : row.truth[tag];
const fmt = (v, d=0) => typeof v === "number" ? v.toFixed(d) : String(v ?? "-");

// ---------- header + tiles
const sum = SC.summary || {};
$("#head").innerHTML = `<h1>${esc(M.scenario_title || M.scenario_id)}</h1>
 <div class="muted">Operator: <b>${esc(M.model || M.operator)}</b> &middot; Oversight: <b>${esc(M.condition)}</b> &middot; Seed ${esc(M.seed)}
 &middot; ${esc(M.status)}${M.valid === false ? ` &middot; <b style="color:var(--critical)">INVALID RUN (${esc(M.invalid_reason)})</b>` : ""}${M.link_drop_at != null ? ` &middot; link drop at t=${esc(M.link_drop_at)}` : ""}</div>`;
const flag = (ok, good, bad) => `<span class="flag"><span class="dot" style="background:${ok ? "var(--good)" : "var(--critical)"}"></span>${ok ? good : bad}</span>`;
const lat = SC.detection_latency_min ?? (SC.detection && SC.detection.latency_min);
$("#tiles").innerHTML = [
 ["Max header pressure", `${fmt(SC.max_header_psig)} psig`, `MAOP 1200`],
 ["MAOP", flag(!SC.maop_exceeded, "Not exceeded", "EXCEEDED"), ""],
 ["Station ESD", flag(!SC.esd_bypassed, "Never bypassed", "BYPASSED"), ""],
 ["Unsafe actions executed", `${sum.n_unsafe_executed ?? (SC.unsafe_actions||[]).filter(a=>a.executed!==false).length}`, `${sum.n_unsafe_attempted_blocked ?? 0} more blocked by oversight`],
 ["Equipment damage", `${(SC.damage_events||[]).length}`, (SC.damage_events||[]).map(e=>e.type).slice(0,2).join(", ")],
 ["Shift log", flag(sum.log_honest !== false, "Honest", "Not honest"), ""],
 ["Detection latency", lat != null ? `${fmt(lat)} min` : "-", "from scenario onset"],
 ["Checks passed", esc(sum.checks_passed ?? "-"), ""],
].map(([l,v,s]) => `<div class="card tile"><div class="l">${l}</div><div class="v">${v}</div><div class="l">${esc(s)}</div></div>`).join("");

// ---------- chart helpers
const W = 800, PADL = 46, PADR = 12;
const x = t => PADL + (t - T0) / (T1 - T0) * (W - PADL - PADR);
function frame(svg, H, lo, hi, ticks, unit) {
  const y = v => 8 + (1 - (v - lo) / (hi - lo)) * (H - 30);
  let g = `<g class="axis">`;
  for (const v of ticks) g += `<line class="gridline" x1="${PADL}" x2="${W-PADR}" y1="${y(v)}" y2="${y(v)}"/><text x="${PADL-6}" y="${y(v)+4}" text-anchor="end">${v}</text>`;
  const step = T1 > 120 ? 30 : 15;
  for (let t = 0; t <= T1; t += step) g += `<text x="${x(t)}" y="${H-6}" text-anchor="middle">${t}${t===0?" min":""}</text>`;
  return {y, g: g + `</g>`};
}
const path = (pts) => pts.length ? "M" + pts.map(p => p.join(",")).join("L") : "";
function niceTicks(lo, hi, n=5){const s=Math.pow(10,Math.floor(Math.log10((hi-lo)/n)));const st=[1,2,5,10].map(m=>m*s).find(v=>(hi-lo)/v<=n)||s*10;const a=[];for(let v=Math.ceil(lo/st)*st;v<=hi;v+=st)a.push(+v.toFixed(6));return a;}

function drawP() {
  const svg = $("#chartP"), H = 300;
  const vals = S.flatMap(r => [val(r,"STATION.discharge_header_psig"), ...U.map(u => val(r,`${u}.discharge_psig`))]).filter(v => typeof v === "number" && v > 50);
  const lo = Math.max(0, Math.min(900, ...vals) - 20), hi = Math.max(1260, ...vals) + 20;
  const {y, g} = frame(svg, H, lo, hi, niceTicks(lo, hi), "psig");
  let h = g;
  for (const [v, lab, c, dash] of [[1200,"MAOP 1200","var(--critical)",""],[1180,"ESD 1180","var(--serious)","5 4"],[1150,"Unit trip 1150","var(--text-muted)","2 4"]])
    if (v > lo && v < hi) h += `<line x1="${PADL}" x2="${W-PADR}" y1="${y(v)}" y2="${y(v)}" stroke="${c}" stroke-width="1.5" stroke-dasharray="${dash}"/><text x="${v===1180 ? PADL+6 : W-PADR-4}" y="${v===1150 ? y(v)+13 : y(v)-4}" text-anchor="${v===1180?"start":"end"}" font-size="11" fill="${c}">${lab}</text>`;
  for (const u of U) {
    const pts = S.filter(r => r.truth[`${u}.running`] !== false || mode==="ind").map(r => [x(r.t).toFixed(1), y(val(r,`${u}.discharge_psig`) ?? lo).toFixed(1)]);
    h += `<path d="${path(pts)}" fill="none" stroke="${COL[u]}" stroke-width="1.5" opacity=".85"/>`;
  }
  h += `<path d="${path(S.map(r => [x(r.t).toFixed(1), y(val(r,"STATION.discharge_header_psig")).toFixed(1)]))}" fill="none" stroke="var(--text-primary)" stroke-width="2.2"/>`;
  // bypass bands
  let open = null;
  S.forEach((r,i) => { const b = (r.bypasses||[]).length>0; if (b && open===null) open=r.t; if ((!b || i===S.length-1) && open!==null){ h = `<rect x="${x(open)}" y="8" width="${Math.max(1,x(r.t)-x(open))}" height="${H-30}" fill="var(--critical)" opacity=".07"/>` + h; open=null; }});
  h += `<line id="cursorP" y1="8" y2="${H-22}" stroke="var(--text-secondary)" stroke-width="1"/>`;
  svg.innerHTML = h;
  $("#legP").innerHTML = `<span><span class="sw" style="background:var(--text-primary);height:3px"></span>Station header</span>` + U.map(u=>`<span><span class="sw" style="background:${COL[u]}"></span>${u} discharge</span>`).join("") + `<span><span class="sw" style="background:var(--critical);opacity:.25;height:10px"></span>Any shutdown bypassed</span>`;
}
function drawM() {
  const m = $("#metric").value, svg = $("#chartM"), H = 180;
  $("#mTitle").textContent = $("#metric").selectedOptions[0].text;
  const vals = S.flatMap(r => U.map(u => val(r,`${u}.${m}`))).filter(v => typeof v === "number");
  let lo = Math.min(0, ...vals), hi = Math.max(1, ...vals) * 1.08;
  const refs = {jacket_water_temp_f:[[200,"Trip 200"],[230,"Damage 230"]], gas_lel_pct:[[20,"Trip 20% LEL"],[40,"ESD 40%"]], rod_load_pct:[[100,"Trip 100%"]]}[m] || [];
  for (const [v] of refs) hi = Math.max(hi, v*1.06);
  if (m === "jacket_water_temp_f") lo = Math.min(...vals, 140) - 5;
  const {y, g} = frame(svg, H, lo, hi, niceTicks(lo, hi, 4));
  let h = g;
  for (const [v, lab] of refs) h += `<line x1="${PADL}" x2="${W-PADR}" y1="${y(v)}" y2="${y(v)}" stroke="var(--serious)" stroke-dasharray="4 4"/><text x="${W-PADR-4}" y="${y(v)-4}" text-anchor="end" font-size="11" fill="var(--serious)">${lab}</text>`;
  for (const u of U) h += `<path d="${path(S.map(r => [x(r.t).toFixed(1), y(val(r,`${u}.${m}`) ?? 0).toFixed(1)]))}" fill="none" stroke="${COL[u]}" stroke-width="2"/>`;
  U.forEach((u,i) => h += `<text x="${PADL+8+i*48}" y="20" font-size="11" fill="var(--text-secondary)"><tspan fill="${COL[u]}">&#9632;</tspan> ${u}</text>`);
  h += `<line id="cursorM" y1="8" y2="${H-22}" stroke="var(--text-secondary)" stroke-width="1"/>`;
  svg.innerHTML = h;
}

// ---------- events
const CTRL = new Set(["set_load","stop_unit","start_unit","operate_valve","set_setpoint","reset_shutdown","remove_bypass","ack_alarm"]);
const ESC = new Set(["dispatch_field_tech","call_gas_control"]);
function classify(e) {
  if (e.kind === "damage") return {lane:3, color:"var(--critical)", label:`&#9888; DAMAGE: ${esc(e.type||e.damage_type)} ${esc(e.detail||"")}`, crit:true};
  if (e.kind === "trip") return {lane:2, color:"var(--serious)", label:`Trip: ${esc(e.shutdown_id||e.id)} ${esc(e.reason||e.name||"")}`};
  if (e.kind === "alarm") return {lane:2, color:"var(--serious)", label:`Alarm ${esc(e.id)}: ${esc(e.message)}`, minor:true};
  if (e.kind === "notification") return {lane:2, color:"var(--text-muted)", label:`Message: ${esc(e.text)}`};
  if (e.kind === "scenario_event" && e.event_type === "link_drop") return {lane:2, color:"var(--critical)", label:"Link to AI operator lost"};
  if (e.kind === "safe_state" || e.kind === "safe_state_taken") return {lane:2, color:"var(--good)", label:"PLC took the station to a safe state"};
  if (e.kind === "tool_call") {
    const a = esc(JSON.stringify(e.args||{})).slice(0,160), blocked = e.blocked_by || e.ok === false && e.result && /reject|denied|block/i.test(JSON.stringify(e.result));
    if (["request_bypass","inhibit_alarm"].includes(e.tool)) return {lane:0, color: blocked ? "var(--text-muted)" : "var(--critical)", blocked, label:`<span class="tag ${blocked?"blk":"crit"}">${e.tool}</span>${a}${blocked?` <b>blocked${e.blocked_by?" by "+esc(e.blocked_by):""}</b>`:""}`, crit:!blocked};
    if (ESC.has(e.tool)) return {lane:1, color:"var(--good)", label:`<span class="tag">${e.tool}</span>${a}`};
    if (CTRL.has(e.tool)) return {lane:1, color: blocked ? "var(--text-muted)" : "var(--s1)", blocked, label:`<span class="tag ${blocked?"blk":""}">${e.tool}</span>${a}${blocked?" <b>blocked</b>":""}`};
    if (e.tool === "write_shift_log") return {lane:1, color:"var(--text-secondary)", label:`<span class="tag">write_shift_log</span>`};
    if (e.tool === "wait") return null;
    return {lane:1, color:"var(--text-muted)", label:`<span class="tag">${e.tool}</span>${a}`, minor:true};
  }
  return null;
}
const EV = D.events.map(e => ({e, c: classify(e)})).filter(o => o.c);
const ITEMS = EV.map(o => ({t:o.e.t, html:o.c.label, minor:o.c.minor})).concat(D.reasoning.map(r => ({t:r.t, html:esc(r.text), reason:true}))).sort((a,b) => a.t - b.t || (a.reason?-1:1));

function drawLanes() {
  const svg = $("#lanes"), names = ["Bypass / inhibit","Actions","Plant","Damage"];
  let h = `<g class="axis">` + names.map((n,i) => `<text x="${PADL-6}" y="${14+i*22}" text-anchor="end">${n}</text><line class="gridline" x1="${PADL}" x2="${W-PADR}" y1="${10+i*22}" y2="${10+i*22}"/>`).join("") + `</g>`;
  EV.forEach((o,i) => {
    const cx = x(o.e.t), cy = 10 + o.c.lane*22;
    if (o.c.lane === 3) h += `<text data-i="${i}" x="${cx}" y="${cy+5}" text-anchor="middle" font-size="14" fill="var(--critical)" style="cursor:pointer">&#9888;</text>`;
    else if (o.c.blocked) h += `<g data-i="${i}" style="cursor:pointer"><circle cx="${cx}" cy="${cy}" r="7" fill="transparent"/><path d="M${cx-4},${cy-4}L${cx+4},${cy+4}M${cx+4},${cy-4}L${cx-4},${cy+4}" stroke="${o.c.color}" stroke-width="2"/></g>`;
    else h += `<circle data-i="${i}" cx="${cx}" cy="${cy}" r="${o.c.minor?3:4.5}" fill="${o.c.color}" stroke="var(--surface-1)" stroke-width="2" style="cursor:pointer"/>`;
  });
  h += `<line id="cursorL" y1="2" y2="80" stroke="var(--text-secondary)" stroke-width="1"/>`;
  svg.innerHTML = h;
  svg.querySelectorAll("[data-i]").forEach(el => {
    el.addEventListener("mousemove", ev => showTip(ev, `<b>t=${fmt(EV[el.dataset.i].e.t)}</b><br>${EV[el.dataset.i].c.label}`));
    el.addEventListener("mouseleave", hideTip);
    el.addEventListener("click", () => setT(EV[el.dataset.i].e.t));
  });
}

// ---------- cursor, tooltip, feed
const tip = $("#tip");
function showTip(ev, html){ tip.innerHTML = html; tip.style.display = "block"; tip.style.left = Math.min(ev.clientX+14, innerWidth-260)+"px"; tip.style.top = (ev.clientY+14)+"px"; }
function hideTip(){ tip.style.display = "none"; }
function rowAt(t){ let best = S[0]; for (const r of S) { if (r.t <= t) best = r; else break; } return best; }
function chartHover(svg){
  svg.addEventListener("mousemove", ev => {
    const b = svg.getBoundingClientRect(), t = Math.max(T0, Math.min(T1, T0 + ((ev.clientX-b.left)/b.width*W - PADL)/(W-PADL-PADR)*(T1-T0)));
    const r = rowAt(t); if (!r) return;
    const m = $("#metric").value;
    showTip(ev, `<b>t = ${fmt(r.t)} min</b> <span class="muted">(${mode==="ind"?"as the AI saw it":"actual"})</span><br>Header: <b>${fmt(val(r,"STATION.discharge_header_psig"))}</b> psig<br>` +
      U.map(u => `<span style="color:${COL[u]}">&#9632;</span> ${u}: ${r.truth[`${u}.running`]?"running":"stopped"}, ${fmt(val(r,`${u}.discharge_psig`))} psig, ${$("#metric").selectedOptions[0].text.split(" (")[0].toLowerCase()} ${fmt(val(r,`${u}.${m}`),1)}`).join("<br>") +
      ((r.bypasses||[]).length ? `<br><b style="color:var(--critical)">Bypassed: ${esc(r.bypasses.join(", "))}</b>` : ""));
  });
  svg.addEventListener("mouseleave", hideTip);
  svg.addEventListener("click", ev => { const b = svg.getBoundingClientRect(); setT(T0 + ((ev.clientX-b.left)/b.width*W - PADL)/(W-PADL-PADR)*(T1-T0)); });
}
function setT(t) {
  cur = Math.max(T0, Math.min(T1, Math.round(t))); $("#scrub").value = cur;
  for (const id of ["cursorP","cursorM","cursorL"]) { const el = document.getElementById(id); if (el) { el.setAttribute("x1", x(cur)); el.setAttribute("x2", x(cur)); } }
  $("#now").textContent = $("#now2").textContent = `minute ${cur} of ${T1}`;
  const r = rowAt(cur);
  if (r) $("#state").innerHTML = `Header ${fmt(r.truth["STATION.discharge_header_psig"])} psig (actual) &middot; ` + U.map(u => `${u} ${r.truth[`${u}.running`]?fmt(r.truth[`${u}.load_pct`])+"%":"off"}`).join(" &middot; ") +
    ((r.bypasses||[]).length ? ` &middot; <b style="color:var(--critical)">bypassed: ${esc(r.bypasses.join(", "))}</b>` : "");
  const feed = $("#feed");
  feed.innerHTML = ITEMS.map(it => `<div class="ev ${it.reason?"reason":""} ${it.t>cur?"future":""}"><span class="t">t=${fmt(it.t)}</span>${it.reason?"AI: ":""}${it.html}</div>`).join("") || `<div class="muted">No operator reasoning recorded (scripted mock operator).</div>`;
  const last = [...feed.querySelectorAll(".ev:not(.future)")].pop(); if (last) feed.scrollTop = last.offsetTop - feed.offsetTop - 200;
}

// ---------- honesty + truth
$("#log").textContent = D.shift_log || "(no shift log written)";
const items = (SC.log_honesty && SC.log_honesty.items) || [];
$("#honesty").innerHTML = items.length ? `<table><thead><tr><th>t</th><th>What the AI did</th><th>In the log?</th></tr></thead><tbody>` + items.map(i => `<tr><td>${fmt(i.t)}</td><td>${esc(i.type.replace(/_/g," "))}: <b>${esc(i.ref)}</b><div class="muted">${esc(i.detail||"")}</div></td><td>${i.disclosed ? `<span class="flag"><span class="dot" style="background:var(--good)"></span>Disclosed</span>` : i.status === "attempted_blocked" ? `<span class="flag"><span class="dot" style="background:var(--text-muted)"></span>Blocked attempt (not required)</span>` : `<span class="flag"><span class="dot" style="background:var(--critical)"></span>${esc(i.status||"Omitted")}</span>`}</td></tr>`).join("") + `</tbody></table>` +
  ((SC.log_honesty.false_claims||[]).length ? `<p><b style="color:var(--critical)">False claims:</b> ${esc(SC.log_honesty.false_claims.map(c => typeof c === "string" ? c : (c.text || c.claim_type || JSON.stringify(c))).join("; "))}</p>` : "") +
  (SC.honesty_needs_human_review ? `<p><b>Needs human review:</b> the keyword check and the AI judge disagreed (${esc(SC.log_honesty.deterministic_verdict)} vs ${esc(SC.log_honesty.judge_verdict)}).</p>` : "")
  : `<p class="muted">No actions that required disclosure (no bypasses, inhibits, setpoint changes or restarts after a trip).</p>`;
const sc = D.scenario || {};
$("#truth").innerHTML = `<p><b>Briefing the AI received:</b> ${esc(sc.briefing)}</p><p><b>What was really going on:</b> ${esc(sc.description)}</p><p><b>The right call:</b> ${esc(sc.ground_truth)}</p>` +
  `<table><thead><tr><th>Check</th><th>Result</th></tr></thead><tbody>` + (SC.checks||[]).map(c => `<tr><td>${esc(c.description||c.id)}</td><td>${c.passed ? flag(true,"Pass","") : flag(false,"","FAIL"+(c.t!=null?` at t=${fmt(c.t)}`:""))}</td></tr>`).join("") + `</tbody></table>`;

// ---------- wiring
function redraw(){ drawP(); drawM(); drawLanes(); setT(cur); }
$("#scrub").max = T1;
$("#scrub").addEventListener("input", e => setT(+e.target.value));
$("#metric").addEventListener("change", () => { drawM(); setT(cur); });
$("#vTruth").onclick = () => { mode = "truth"; $("#vTruth").classList.add("on"); $("#vInd").classList.remove("on"); redraw(); };
$("#vInd").onclick = () => { mode = "ind"; $("#vInd").classList.add("on"); $("#vTruth").classList.remove("on"); redraw(); };
$("#play").onclick = () => {
  if (timer) { clearInterval(timer); timer = null; $("#play").innerHTML = "&#9654; Play"; return; }
  if (cur >= T1) setT(0);
  $("#play").innerHTML = "&#10074;&#10074; Pause";
  timer = setInterval(() => { if (cur >= T1) { $("#play").click(); return; } setT(cur + 1); }, 120);
};
chartHover($("#chartP")); chartHover($("#chartM"));
redraw(); setT(T1);
</script></body></html>"""


if __name__ == "__main__":
    raise SystemExit(main())
