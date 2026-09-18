"""Render the report card: Markdown for the repo, HTML for everyone else."""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import rubric

STATUS_LABEL = {
    "ship": "Ship it",
    "ship_with_guardrails": "Ship with guardrails",
    "not_ready": "Not ready",
}


def _ms(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:,.0f} ms"


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"

def _exp(m: Dict[str, Any], expectation: str, field: str):
    return (m.get("barge_in_by_expectation", {}) or {}).get(expectation, {}).get(field)



# ------------------------------------------------------------------- markdown --


def markdown(result: Dict[str, Any], measured: Dict[str, Any],
             manifest: Dict[str, Any]) -> str:
    profile = manifest.get("profile", "default")
    w = rubric.weights(profile)
    systems = result.get("systems", [])
    ids = [s["id"] for s in systems]
    L: List[str] = []

    L.append(f"# Voice agent report card — `{manifest.get('run_id', '')}`")
    L.append("")
    L.append(f"*{manifest.get('battery_name', '')} · profile `{profile}` · "
             f"{manifest.get('created', '')}*")
    L.append("")
    L.append("## Verdict")
    L.append("")
    L.append(result.get("verdict", "_no verdict returned_"))
    L.append("")

    # --- scorecard ---------------------------------------------------------
    L.append("## Scorecard")
    L.append("")
    L.append("| Dimension | Weight | " + " | ".join(ids) + " |")
    L.append("|---|---:|" + "---:|" * len(ids))
    by_sys = {s["id"]: {d["key"]: d for d in s.get("dimensions", [])} for s in systems}
    for d in rubric.DIMENSIONS:
        row = [f"| **{d.name}** | {w[d.key]} "]
        for sid in ids:
            sc = by_sys[sid].get(d.key, {}).get("score")
            row.append(f"| {'—' if sc is None else f'{sc:g}'} ")
        L.append("".join(row) + "|")
    L.append("| **Total / 100** | 100 | "
             + " | ".join(f"**{s.get('total', 0):.1f}**" for s in systems) + " |")
    L.append("| **Grade** |  | "
             + " | ".join(f"**{s.get('grade', '—')}**" for s in systems) + " |")
    L.append("")

    # --- head to head ------------------------------------------------------
    h2h = result.get("head_to_head", [])
    if h2h:
        L.append("## The four measures that decide phone calls")
        L.append("")
        L.append("| Measure | " + " | ".join(ids) + " | Comment |")
        L.append("|---|" + "---|" * len(ids) + "---|")
        for row in h2h:
            vals = {v["system"]: v["value"] for v in row.get("values", [])}
            L.append(f"| {row['measure']} | "
                     + " | ".join(vals.get(i, "—") for i in ids)
                     + f" | {row.get('comment', '')} |")
        L.append("")

    # --- measured ----------------------------------------------------------
    L.append("## Measured (from the waveforms, not the model)")
    L.append("")
    L.append("| | " + " | ".join(ids) + " |")
    L.append("|---|" + "---:|" * len(ids))
    def mrow(label, fn):
        L.append(f"| {label} | " + " | ".join(
            fn(measured.get(i, {})) for i in ids) + " |")
    mrow("Calls analyzed", lambda m: str(m.get("calls", 0)))
    mrow("Turn latency median", lambda m: _ms(m.get("latency", {}).get("median_ms")))
    mrow("Turn latency p90", lambda m: _ms(m.get("latency", {}).get("p90_ms")))
    mrow("Turn latency worst", lambda m: _ms(m.get("latency", {}).get("max_ms")))
    mrow("Latency spread (IQR)", lambda m: _ms(m.get("latency", {}).get("iqr_ms")))
    mrow("Barge-in stop median", lambda m: _ms(m.get("barge_in", {}).get("stop_median_ms")))
    mrow("Barge-in stop p90", lambda m: _ms(m.get("barge_in", {}).get("stop_p90_ms")))
    mrow("Yield rate on real interrupts", lambda m: _pct(_exp(m, "yield", "yield_rate")))
    mrow("FALSE-stop rate (S11)", lambda m: _pct(_exp(m, "hold", "false_stop_rate")))
    mrow("Times it cut the caller off", lambda m: str(m.get("agent_interruptions", "—")))
    mrow("Agent talk ratio", lambda m: _pct(m.get("agent_talk_ratio")))
    mrow("Mean agent turn", lambda m: "—" if m.get("mean_agent_turn_s") is None
         else f"{m['mean_agent_turn_s']:.1f} s")
    mrow("Longest dead air", lambda m: "—" if m.get("longest_dead_air_s") is None
         else f"{m['longest_dead_air_s']:.1f} s")
    L.append("")

    # --- per system --------------------------------------------------------
    for s in systems:
        sid = s["id"]
        dep = s.get("deployability", {})
        L.append(f"## System {sid} — {s.get('grade', '?')} "
                 f"({s.get('total', 0):.1f}/100)")
        L.append("")
        L.append(f"*{s.get('sketch', '')}*")
        L.append("")
        L.append(f"**Deployability:** {STATUS_LABEL.get(dep.get('status', ''), '?')}")
        for g in dep.get("guardrails", []):
            L.append(f"  - {g}")
        L.append("")
        if s.get("strengths"):
            L.append("**Strengths**")
            L += [f"- {x}" for x in s["strengths"]] + [""]
        if s.get("weaknesses"):
            L.append("**Weaknesses**")
            L += [f"- {x}" for x in s["weaknesses"]] + [""]
        wm = s.get("worst_moment") or {}
        if wm.get("quote"):
            L.append(f"**Worst moment** ({wm.get('scenario', '?')})")
            L.append("")
            L.append("> " + wm["quote"].replace("\n", "\n> "))
            L.append("")
            L.append(wm.get("why", ""))
            L.append("")
        L.append("<details><summary>Per-dimension reasoning</summary>")
        L.append("")
        for d in rubric.DIMENSIONS:
            e = by_sys[sid].get(d.key)
            if not e:
                continue
            sc = e.get("score")
            L.append(f"**{d.name} — {'—' if sc is None else f'{sc:g}'}/5**")
            L.append("")
            L.append(e.get("rationale", ""))
            for ev in e.get("evidence", []):
                L.append(f"- `{ev}`" if len(ev) < 120 else f"- {ev}")
            L.append("")
        L.append("</details>")
        L.append("")

    # --- taxonomy ----------------------------------------------------------
    tax = result.get("failure_taxonomy", [])
    if tax:
        L.append("## Failure taxonomy")
        L.append("")
        L.append("| Severity | Root cause | Failure | Systems | Fixable |")
        L.append("|---|---|---|---|---|")
        order = {"critical": 0, "major": 1, "minor": 2}
        for f in sorted(tax, key=lambda x: order.get(x.get("severity"), 3)):
            sev = f.get("severity", "")
            mark = "**CRITICAL**" if sev == "critical" else sev
            L.append(f"| {mark} | {f.get('root_cause', '')} | {f.get('failure', '')} "
                     f"| {', '.join(f.get('systems', []))} | {f.get('fixable', '')} |")
        L.append("")

    gaps = result.get("methodology_gaps", [])
    if gaps:
        L.append("## What this test did not cover")
        L.append("")
        L += [f"- {g}" for g in gaps] + [""]

    bg = result.get("blind_guess", [])
    if bg:
        L.append("## Blind guess (speculative — written last, scores nothing)")
        L.append("")
        L.append("| System | Guess | Confidence | Reasoning |")
        L.append("|---|---|---|---|")
        for g in bg:
            L.append(f"| {g.get('system')} | {g.get('guess', '')} "
                     f"| {g.get('confidence', '')} | {g.get('reasoning', '')} |")
        L.append("")

    meta = result.get("_meta", {})
    L.append("---")
    L.append("")
    L.append(f"*Scored by `{meta.get('model', '?')}` at effort "
             f"`{meta.get('effort', '?')}`. Measured numbers come from "
             f"`earshot.audio`, not from the model. "
             f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}.*")
    return "\n".join(L)


# ----------------------------------------------------------------------- html --

_CSS = """
:root{
  --ground:#f4f5f7; --surface:#ffffff; --sunk:#eceef2;
  --ink:#14181f; --mut:#5d6773; --faint:#8b95a3;
  --line:#dde2e9; --rule:#c6cdd7;
  --accent:#b8530a; --accent-soft:#f0e2d4;
  --good:#2f7d5b; --warn:#9a6b10; --crit:#b03a2e;
  --track:#e2e6ec;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#101318; --surface:#171b22; --sunk:#1d222b;
  --ink:#e6e9ee; --mut:#8b95a3; --faint:#6b7684;
  --line:#262c36; --rule:#39424f;
  --accent:#ff9d3d; --accent-soft:#3a2a18;
  --good:#57b894; --warn:#d9a441; --crit:#e8796d;
  --track:#232a34;
}}
:root[data-theme="dark"]{
  --ground:#101318; --surface:#171b22; --sunk:#1d222b;
  --ink:#e6e9ee; --mut:#8b95a3; --faint:#6b7684;
  --line:#262c36; --rule:#39424f;
  --accent:#ff9d3d; --accent-soft:#3a2a18;
  --good:#57b894; --warn:#d9a441; --crit:#e8796d;
  --track:#232a34;
}
*{box-sizing:border-box}
body{
  background:var(--ground); color:var(--ink); margin:0;
  font:400 15.5px/1.65 "IBM Plex Sans","Helvetica Neue",Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:62rem;margin:0 auto;padding:3.5rem 1.5rem 7rem}
.mono,td.n,th.n,.grade,.scale-axis span,.val{
  font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  font-variant-numeric:tabular-nums;
}

/* ---- masthead ---- */
.eyebrow{
  font-family:"IBM Plex Mono",monospace; font-size:.7rem; letter-spacing:.18em;
  text-transform:uppercase; color:var(--accent); margin-bottom:.9rem;
}
h1{
  font-family:"IBM Plex Sans Condensed","IBM Plex Sans",sans-serif;
  font-weight:600; font-size:clamp(2rem,4.5vw,2.9rem); line-height:1.08;
  letter-spacing:-.02em; margin:0 0 .5rem; text-wrap:balance;
}
.meta{
  font-family:"IBM Plex Mono",monospace; font-size:.76rem; color:var(--mut);
  display:flex; flex-wrap:wrap; gap:.4rem 1.1rem; margin-bottom:2.5rem;
  padding-bottom:1.5rem; border-bottom:2px solid var(--rule);
}
h2{
  font-family:"IBM Plex Sans Condensed","IBM Plex Sans",sans-serif;
  font-weight:600; font-size:1.3rem; letter-spacing:-.005em;
  margin:3.25rem 0 1rem; padding-bottom:.45rem;
  border-bottom:1px solid var(--rule);
}
h3{
  font-family:"IBM Plex Sans Condensed","IBM Plex Sans",sans-serif;
  font-weight:600; font-size:.95rem; margin:1.4rem 0 .35rem;
}
p{margin:.5rem 0 .9rem; max-width:64ch}

/* ---- verdict ---- */
.verdict{
  background:var(--surface); border:1px solid var(--line);
  border-left:3px solid var(--accent); padding:1.35rem 1.5rem;
  font-size:1.05rem; line-height:1.6;
}
.verdict p{margin:0;max-width:none}

/* ---- tables ---- */
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.88rem;min-width:32rem}
th,td{padding:.55rem .7rem;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
thead th{
  font-family:"IBM Plex Mono",monospace; font-weight:500; color:var(--mut);
  font-size:.68rem; text-transform:uppercase; letter-spacing:.1em;
  white-space:nowrap; border-bottom:1px solid var(--rule);
}
td.n,th.n{text-align:right;white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
tr.total td{border-top:2px solid var(--rule);border-bottom:none;font-weight:600;padding-top:.7rem}
.sublabel{display:block;font-size:.73rem;color:var(--faint);font-weight:400;margin-top:.1rem;max-width:34ch}

/* score cells */
.cell{display:flex;flex-direction:column;align-items:flex-end;gap:.28rem}
.pips{display:flex;gap:2px}
.pips i{width:6px;height:12px;background:var(--track);border-radius:1px}
.pips i.on{background:var(--accent)}
.pips i.null{background:repeating-linear-gradient(45deg,var(--track),var(--track) 2px,transparent 2px,transparent 4px)}

/* ---- millisecond scale: the one chart this subject actually needs ---- */
.scale{background:var(--surface);border:1px solid var(--line);padding:1.4rem 1.5rem 1rem}
.scale-row{display:grid;grid-template-columns:2.5rem 1fr;gap:.9rem;align-items:center;margin-bottom:1.3rem}
.scale-row:first-of-type{margin-top:.5rem}
.scale-row b{font-family:"IBM Plex Sans Condensed",sans-serif;font-size:1.05rem}
.track{position:relative;height:30px;background:var(--sunk);border-radius:2px}
.track .bar{position:absolute;left:0;top:7px;height:16px;background:var(--accent);opacity:.85;border-radius:2px}
.track .mark{position:absolute;top:0;bottom:0;width:2px;background:var(--ink);z-index:3}
.track .mark.p90{opacity:.7}
.track .mark.max{opacity:.4}
.track .lbl{
  position:absolute;top:-1.2rem;font-family:"IBM Plex Mono",monospace;
  font-size:.65rem;color:var(--mut);transform:translateX(-50%);white-space:nowrap;
}
.track .lbl.tick{color:var(--faint);font-size:.6rem}
.thr{position:absolute;top:-4px;bottom:-4px;width:0;
     border-left:1px dashed var(--crit);opacity:.65;z-index:4}
.thr-key{display:flex;flex-wrap:wrap;gap:.3rem 1.2rem;margin:.7rem 0 0 3.4rem;
         font-family:"IBM Plex Mono",monospace;font-size:.63rem;color:var(--mut)}
.thr-key i{display:inline-block;width:0;height:.7em;vertical-align:-1px;
           border-left:1px dashed var(--crit);margin-right:.35rem;opacity:.8}
.scale-axis{position:relative;height:1.4rem;margin-left:3.4rem;border-top:1px solid var(--line)}
.scale-axis span{position:absolute;top:.25rem;font-size:.65rem;color:var(--faint);transform:translateX(-50%)}
.scale-axis span:first-child{transform:none}
.scale-key{font-size:.73rem;color:var(--mut);margin:.4rem 0 0 3.4rem}
.scale-key b{color:var(--ink);font-weight:500}

/* ---- system cards ---- */
.card{background:var(--surface);border:1px solid var(--line);padding:1.5rem 1.6rem;margin:1rem 0}
.hdr{display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap;
     padding-bottom:1rem;margin-bottom:1rem;border-bottom:1px solid var(--line)}
.grade{font-size:2.6rem;font-weight:600;line-height:1;letter-spacing:-.04em}
.score{font-size:.95rem;color:var(--mut)}
.chip{
  font-family:"IBM Plex Mono",monospace;font-size:.68rem;letter-spacing:.08em;
  text-transform:uppercase;padding:.25rem .6rem;border:1px solid currentColor;
  border-radius:2px;color:var(--mut);
}
.chip.ship{color:var(--good)} .chip.not_ready{color:var(--crit)}
.chip.ship_with_guardrails{color:var(--warn)}
blockquote{
  margin:.7rem 0;padding:.7rem 1rem;background:var(--sunk);
  border-left:2px solid var(--accent);font-size:.95rem;
}
ul{margin:.3rem 0 1rem;padding-left:1.15rem} li{margin:.28rem 0;max-width:64ch}
code{font-family:"IBM Plex Mono",monospace;font-size:.82em;background:var(--sunk);
     padding:.1rem .35rem;border-radius:2px}
details{margin:1rem 0;border-top:1px solid var(--line);padding-top:.8rem}
summary{cursor:pointer;font-family:"IBM Plex Mono",monospace;font-size:.72rem;
        letter-spacing:.08em;text-transform:uppercase;color:var(--mut)}
summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}

/* ---- severity ---- */
td.sev{font-family:"IBM Plex Mono",monospace;font-size:.68rem;text-transform:uppercase;
       letter-spacing:.08em;white-space:nowrap;border-left:3px solid transparent}
td.sev.critical{color:var(--crit);border-left-color:var(--crit);font-weight:600}
td.sev.major{color:var(--warn);border-left-color:var(--warn)}
td.sev.minor{color:var(--mut);border-left-color:var(--line)}

.note{background:var(--accent-soft);border:1px solid var(--line);padding:1rem 1.2rem;
      font-size:.9rem;margin:1rem 0}
.note b{font-family:"IBM Plex Sans Condensed",sans-serif}
footer{margin-top:4.5rem;padding-top:1.2rem;border-top:2px solid var(--rule);
       color:var(--mut);font-size:.78rem;font-family:"IBM Plex Mono",monospace}
footer a{color:var(--accent)}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media print{body{background:#fff}.card,.scale{break-inside:avoid}}
"""


def _esc(s: Any) -> str:
    return html.escape(str(s or ""))




def _pips(score: Optional[float]) -> str:
    """Five cells; filled to the score. A null score is hatched, not blank -
    'not measured' and 'measured as zero' must not look alike."""
    if score is None:
        return "<span class=pips>" + "<i class=null></i>" * 5 + "</span>"
    n = int(round(score))
    return "<span class=pips>" + "".join(
        f"<i class={'on' if i < n else ''}></i>" for i in range(5)) + "</span>"


def _latency_scale(measured: Dict[str, Any], ids: List[str]) -> str:
    """Every system's latency plotted on ONE millisecond axis.

    The two dashed thresholds are the numbers that matter on a phone call:
    ~800ms is where a caller starts to notice the gap, ~1500ms is where they
    say 'hello?' into it. A bar that crosses the second line is the finding.
    """
    vals = []
    for i in ids:
        lat = (measured.get(i, {}) or {}).get("latency", {}) or {}
        vals += [v for v in (lat.get("median_ms"), lat.get("p90_ms"),
                             lat.get("max_ms")) if v]
    if not vals:
        return ""
    top = max(max(vals) * 1.12, 2000.0)
    pct = lambda v: min(100.0, v / top * 100.0)

    P = ["<div class=scale>"]
    for i in ids:
        lat = (measured.get(i, {}) or {}).get("latency", {}) or {}
        med, p90, mx = lat.get("median_ms"), lat.get("p90_ms"), lat.get("max_ms")
        if med is None:
            continue
        P.append(f"<div class=scale-row><b>{_esc(i)}</b><div class=track>")
        for t in (800.0, 1500.0):
            if t < top:
                P.append(f"<span class=thr style='left:{pct(t):.1f}%'></span>")
        P.append(f"<span class=bar style='width:{pct(med):.1f}%'></span>")
        P.append(f"<span class=lbl style='left:{pct(med):.1f}%'>{med:,.0f}</span>")
        if p90:
            P.append(f"<span class='mark p90' style='left:{pct(p90):.1f}%'></span>")
        if mx and (not p90 or abs(mx - p90) > top * 0.04):
            P.append(f"<span class='mark max' style='left:{pct(mx):.1f}%'></span>")
            P.append(f"<span class='lbl tick' style='left:{pct(mx):.1f}%'>"
                     f"worst {mx:,.0f}</span>")
        P.append("</div></div>")

    P.append("<div class=scale-axis>")
    step = 500 if top <= 3000 else 1000
    t = 0
    while t <= top:
        P.append(f"<span style='left:{pct(t):.1f}%'>{t:,.0f}</span>")
        t += step
    P.append("</div>")
    P.append("<div class=thr-key><span><i></i>800ms &middot; caller notices the "
             "gap</span><span><i></i>1.5s &middot; caller speaks into it</span></div>")
    P.append("<p class=scale-key>Milliseconds from the caller finishing to the "
             "agent starting. The bar ends at the <b>median</b>; the solid ticks "
             "are <b>p90</b> and <b>worst</b>. How far those ticks sit from the "
             "bar is the jitter &mdash; and jitter is what callers actually "
             "notice.</p>")
    P.append("</div>")
    return "".join(P)


def html_report(result: Dict[str, Any], measured: Dict[str, Any],
                manifest: Dict[str, Any]) -> str:
    profile = manifest.get("profile", "default")
    w = rubric.weights(profile)
    systems = result.get("systems", [])
    ids = [s["id"] for s in systems]
    by_sys = {s["id"]: {d["key"]: d for d in s.get("dimensions", [])} for s in systems}
    P: List[str] = []
    A = P.append

    A("<title>Voice Agent Report Card</title>")
    A("<link rel=preconnect href='https://fonts.googleapis.com'>"
      "<link rel=preconnect href='https://fonts.gstatic.com' crossorigin>"
      "<link rel=stylesheet href='https://fonts.googleapis.com/css2?"
      "family=IBM+Plex+Mono:wght@400;500&"
      "family=IBM+Plex+Sans+Condensed:wght@600&"
      "family=IBM+Plex+Sans:wght@400;500;600&display=swap'>")
    A(f"<style>{_CSS}</style><div class=wrap>")
    A("<div class=eyebrow>Blind comparative test &middot; "
      f"{len(ids)} systems</div>")
    A("<h1>Voice agent report card</h1>")
    A("<div class=meta>"
      f"<span>{_esc(manifest.get('run_id'))}</span>"
      f"<span>{_esc(manifest.get('battery_name'))}</span>"
      f"<span>profile {_esc(profile)}</span>"
      f"<span>{_esc(manifest.get('created'))}</span>"
      "<span>systems identified by letter only</span></div>")
    A(f"<div class=verdict><p>{_esc(result.get('verdict'))}</p></div>")

    A("<h2>Scorecard</h2><div class=scroll><table><thead><tr><th>Dimension</th>"
      "<th class=n>Weight</th>"
      + "".join(f"<th class=n>{_esc(i)}</th>" for i in ids)
      + "</tr></thead><tbody>")
    for d in rubric.DIMENSIONS:
        A(f"<tr><td>{_esc(d.name)}"
          f"<span class=sublabel>{_esc(d.what_it_measures)}</span></td>"
          f"<td class=n>{w[d.key]}</td>")
        for sid in ids:
            sc = by_sys[sid].get(d.key, {}).get("score")
            A(f"<td class=n><span class=cell>"
              f"<span>{'not measured' if sc is None else f'{sc:g}'}</span>"
              f"{_pips(sc)}</span></td>")
        A("</tr>")
    A("<tr class=total><td>Total</td><td class=n>100</td>"
      + "".join(f"<td class=n>{s.get('total', 0):.1f}</td>" for s in systems) + "</tr>")
    A("<tr class=total><td>Grade</td><td class=n></td>"
      + "".join(f"<td class=n>{_esc(s.get('grade'))}</td>" for s in systems)
      + "</tr></tbody></table></div>")

    h2h = result.get("head_to_head", [])
    if h2h:
        A("<h2>The four measures that decide phone calls</h2><div class=scroll>"
          "<table><thead><tr><th>Measure</th>"
          + "".join(f"<th class=n>{_esc(i)}</th>" for i in ids)
          + "<th>Comment</th></tr></thead><tbody>")
        for row in h2h:
            vals = {v["system"]: v["value"] for v in row.get("values", [])}
            A(f"<tr><td>{_esc(row.get('measure'))}</td>"
              + "".join(f"<td class=n>{_esc(vals.get(i, '—'))}</td>" for i in ids)
              + f"<td>{_esc(row.get('comment'))}</td></tr>")
        A("</tbody></table></div>")

    A("<h2>Response latency, to scale</h2>")
    A(_latency_scale(measured, ids))

    A("<h2>Measured from the waveforms</h2><div class=scroll><table><thead><tr><th></th>"
      + "".join(f"<th class=n>{_esc(i)}</th>" for i in ids)
      + "</tr></thead><tbody>")
    rows = [
        ("Calls analyzed", lambda m: str(m.get("calls", 0))),
        ("Turn latency median", lambda m: _ms(m.get("latency", {}).get("median_ms"))),
        ("Turn latency p90", lambda m: _ms(m.get("latency", {}).get("p90_ms"))),
        ("Turn latency worst", lambda m: _ms(m.get("latency", {}).get("max_ms"))),
        ("Latency spread (IQR)", lambda m: _ms(m.get("latency", {}).get("iqr_ms"))),
        ("Barge-in stop median",
         lambda m: _ms(m.get("barge_in", {}).get("stop_median_ms"))),
        ("Barge-in stop p90", lambda m: _ms(m.get("barge_in", {}).get("stop_p90_ms"))),
        ("Yield rate on real interrupts",
         lambda m: _pct(_exp(m, "yield", "yield_rate"))),
        ("FALSE-stop rate (hold scenarios)",
         lambda m: _pct(_exp(m, "hold", "false_stop_rate"))),
        ("Times it cut the caller off",
         lambda m: str(m.get("agent_interruptions", "—"))),
        ("Agent talk ratio", lambda m: _pct(m.get("agent_talk_ratio"))),
        ("Mean agent turn", lambda m: "—" if m.get("mean_agent_turn_s") is None
         else f"{m['mean_agent_turn_s']:.1f} s"),
        ("Longest dead air", lambda m: "—" if m.get("longest_dead_air_s") is None
         else f"{m['longest_dead_air_s']:.1f} s"),
    ]
    for label, fn in rows:
        A(f"<tr><td>{_esc(label)}</td>"
          + "".join(f"<td class=n>{_esc(fn(measured.get(i, {})))}</td>" for i in ids)
          + "</tr>")
    A("</tbody></table></div>")

    for s in systems:
        sid = s["id"]
        dep = s.get("deployability", {})
        st = dep.get("status", "")
        A(f"<h2>System {_esc(sid)}</h2><div class=card>")
        A(f"<div class=hdr><span class='grade mono'>{_esc(s.get('grade'))}</span>"
          f"<span class='score mono'>{s.get('total', 0):.1f}/100</span>"
          f"<span class='chip {_esc(st)}'>{_esc(STATUS_LABEL.get(st, st))}</span></div>")
        A(f"<p>{_esc(s.get('sketch'))}</p>")
        if dep.get("guardrails"):
            A("<h3>Required guardrails</h3><ul>"
              + "".join(f"<li>{_esc(g)}</li>" for g in dep["guardrails"]) + "</ul>")
        if s.get("strengths"):
            A("<h3>Strengths</h3><ul>"
              + "".join(f"<li>{_esc(x)}</li>" for x in s["strengths"]) + "</ul>")
        if s.get("weaknesses"):
            A("<h3>Weaknesses</h3><ul>"
              + "".join(f"<li>{_esc(x)}</li>" for x in s["weaknesses"]) + "</ul>")
        wm = s.get("worst_moment") or {}
        if wm.get("quote"):
            A(f"<h3>Worst moment <span class=chip>{_esc(wm.get('scenario'))}</span></h3>")
            A(f"<blockquote>{_esc(wm['quote'])}</blockquote><p>{_esc(wm.get('why'))}</p>")
        A("<details><summary>Per-dimension reasoning</summary>")
        for d in rubric.DIMENSIONS:
            e = by_sys[sid].get(d.key)
            if not e:
                continue
            sc = e.get("score")
            A(f"<h3>{_esc(d.name)} — {'—' if sc is None else f'{sc:g}'}/5</h3>")
            A(f"<p>{_esc(e.get('rationale'))}</p>")
            if e.get("evidence"):
                A("<ul>" + "".join(f"<li><code>{_esc(ev)}</code></li>"
                                   for ev in e["evidence"]) + "</ul>")
        A("</details></div>")

    tax = result.get("failure_taxonomy", [])
    if tax:
        order = {"critical": 0, "major": 1, "minor": 2}
        A("<h2>Failure taxonomy</h2><div class=scroll><table><thead><tr>"
          "<th>Severity</th><th>Root cause</th><th>Failure</th><th>Systems</th>"
          "<th>Fixable</th></tr></thead><tbody>")
        for f in sorted(tax, key=lambda x: order.get(x.get("severity"), 3)):
            sev = f.get("severity", "")
            A(f"<tr><td class='sev {_esc(sev)}'>{_esc(sev)}</td>"
              f"<td>{_esc(f.get('root_cause'))}</td>"
              f"<td>{_esc(f.get('failure'))}</td>"
              f"<td>{_esc(', '.join(f.get('systems', [])))}</td>"
              f"<td>{_esc(f.get('fixable'))}</td></tr>")
        A("</tbody></table></div>")

    if result.get("methodology_gaps"):
        gaps = result["methodology_gaps"]
        A("<h2>What this test did not cover</h2>")
        A(f"<div class=note><b>Read this before acting on the scores above.</b> "
          f"{_esc(gaps[0])}</div>")
        if len(gaps) > 1:
            A("<ul>" + "".join(f"<li>{_esc(g)}</li>" for g in gaps[1:]) + "</ul>")

    if result.get("blind_guess"):
        A("<h2>Blind guess <span class=chip>speculative · scores nothing</span></h2>"
          "<div class=scroll><table><thead><tr><th>System</th><th>Guess</th>"
          "<th>Confidence</th><th>Reasoning</th></tr></thead><tbody>")
        for g in result["blind_guess"]:
            A(f"<tr><td>{_esc(g.get('system'))}</td><td>{_esc(g.get('guess'))}</td>"
              f"<td>{_esc(g.get('confidence'))}</td>"
              f"<td>{_esc(g.get('reasoning'))}</td></tr>")
        A("</tbody></table></div>")

    meta = result.get("_meta", {})
    A(f"<footer>Scored by <code>{_esc(meta.get('model'))}</code> at effort "
      f"<code>{_esc(meta.get('effort'))}</code>. Measured numbers are computed from "
      f"the waveforms by <code>earshot.audio</code>, not by the model. "
      f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by "
      f"<a href='https://github.com/arunash/earshot'>earshot</a>.</footer></div>")
    return "\n".join(P)
