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
:root{--bg:#fbfaf8;--fg:#1c1b19;--mut:#6b6862;--line:#e3e0da;--card:#fff;
--accent:#1a6b5a;--bad:#a8342a;--warn:#9a6b10;--chip:#f0eee9}
:root:not([data-theme=light]) @media (prefers-color-scheme:dark){}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#16151a;--fg:#e9e7e2;--mut:#9b978f;--line:#2e2c33;--card:#1e1d23;
--accent:#5fc4ab;--bad:#e8796d;--warn:#d9a441;--chip:#26252b}}
:root[data-theme=dark]{--bg:#16151a;--fg:#e9e7e2;--mut:#9b978f;--line:#2e2c33;
--card:#1e1d23;--accent:#5fc4ab;--bad:#e8796d;--warn:#d9a441;--chip:#26252b}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);margin:0;
font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:60rem;margin:0 auto;padding:3rem 1.25rem 6rem}
h1{font-size:1.9rem;letter-spacing:-.02em;margin:0 0 .25rem}
h2{font-size:1.15rem;letter-spacing:-.01em;margin:2.75rem 0 .75rem;
padding-bottom:.4rem;border-bottom:1px solid var(--line)}
h3{font-size:1rem;margin:1.5rem 0 .4rem}
.sub{color:var(--mut);font-size:.85rem;margin-bottom:2rem}
.verdict{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--accent);
border-radius:6px;padding:1.1rem 1.25rem;font-size:1.02rem}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:.88rem;min-width:30rem}
th,td{padding:.5rem .6rem;text-align:left;border-bottom:1px solid var(--line);
vertical-align:top}
th{font-weight:600;color:var(--mut);font-size:.76rem;text-transform:uppercase;
letter-spacing:.05em;white-space:nowrap}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr.total td{border-top:2px solid var(--line);font-weight:700}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:1.25rem 1.4rem;margin:1rem 0}
.hdr{display:flex;align-items:baseline;gap:.75rem;flex-wrap:wrap}
.grade{font-size:2rem;font-weight:700;letter-spacing:-.03em}
.chip{display:inline-block;background:var(--chip);border-radius:99px;
padding:.15rem .6rem;font-size:.76rem;color:var(--mut)}
.chip.ship{color:var(--accent)} .chip.not_ready{color:var(--bad)}
.chip.ship_with_guardrails{color:var(--warn)}
blockquote{margin:.6rem 0;padding:.5rem .9rem;border-left:2px solid var(--line);
color:var(--fg);font-style:italic}
ul{margin:.4rem 0 .9rem;padding-left:1.1rem} li{margin:.2rem 0}
code{background:var(--chip);padding:.1rem .3rem;border-radius:3px;font-size:.85em}
details{margin:.75rem 0} summary{cursor:pointer;color:var(--mut);font-size:.85rem}
.bar{height:5px;background:var(--chip);border-radius:3px;overflow:hidden;
width:100%;min-width:3.5rem;margin-top:.25rem}
.bar>i{display:block;height:100%;background:var(--accent)}
.crit{color:var(--bad);font-weight:700}
footer{margin-top:4rem;color:var(--mut);font-size:.8rem;
border-top:1px solid var(--line);padding-top:1rem}
@media print{body{background:#fff}.card{break-inside:avoid}}
"""


def _esc(s: Any) -> str:
    return html.escape(str(s or ""))


def html_report(result: Dict[str, Any], measured: Dict[str, Any],
                manifest: Dict[str, Any]) -> str:
    profile = manifest.get("profile", "default")
    w = rubric.weights(profile)
    systems = result.get("systems", [])
    ids = [s["id"] for s in systems]
    by_sys = {s["id"]: {d["key"]: d for d in s.get("dimensions", [])} for s in systems}
    P: List[str] = []
    A = P.append

    A(f"<title>Report card — {_esc(manifest.get('run_id', 'earshot'))}</title>")
    A(f"<style>{_CSS}</style><div class=wrap>")
    A(f"<h1>Voice agent report card</h1>")
    A(f"<div class=sub>{_esc(manifest.get('battery_name'))} &middot; profile "
      f"<code>{_esc(profile)}</code> &middot; "
      f"{_esc(manifest.get('created'))} &middot; blind: systems are letters only</div>")
    A(f"<div class=verdict>{_esc(result.get('verdict'))}</div>")

    A("<h2>Scorecard</h2><div class=scroll><table><thead><tr><th>Dimension</th>"
      "<th class=n>Weight</th>"
      + "".join(f"<th class=n>{_esc(i)}</th>" for i in ids)
      + "</tr></thead><tbody>")
    for d in rubric.DIMENSIONS:
        A(f"<tr><td>{_esc(d.name)}<div class=sub style='margin:0;font-size:.74rem'>"
          f"{_esc(d.what_it_measures)}</div></td><td class=n>{w[d.key]}</td>")
        for sid in ids:
            sc = by_sys[sid].get(d.key, {}).get("score")
            pct = 0 if sc is None else sc / 5 * 100
            A(f"<td class=n>{'—' if sc is None else f'{sc:g}'}"
              f"<div class=bar><i style='width:{pct:.0f}%'></i></div></td>")
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
        A(f"<div class=hdr><span class=grade>{_esc(s.get('grade'))}</span>"
          f"<span class=chip>{s.get('total', 0):.1f}/100</span>"
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
            cls = " class=crit" if sev == "critical" else ""
            A(f"<tr><td{cls}>{_esc(sev)}</td><td>{_esc(f.get('root_cause'))}</td>"
              f"<td>{_esc(f.get('failure'))}</td>"
              f"<td>{_esc(', '.join(f.get('systems', [])))}</td>"
              f"<td>{_esc(f.get('fixable'))}</td></tr>")
        A("</tbody></table></div>")

    if result.get("methodology_gaps"):
        A("<h2>What this test did not cover</h2><ul>"
          + "".join(f"<li>{_esc(g)}</li>" for g in result["methodology_gaps"])
          + "</ul>")

    if result.get("blind_guess"):
        A("<h2>Blind guess <span class=chip>speculative — scores nothing</span></h2>"
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
