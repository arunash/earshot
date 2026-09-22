"""Merge several runs into one report.

A single battery answers one shape of question. The channel ladder finds where a
system stops HEARING; the standard battery finds how it BEHAVES. Read alone
either one misleads: the ladder on its own said these two systems were nearly
identical, and the battery on its own could not say whether a noisy car would
kill them. The decision needs both, in one place, scored once.

Merging is not averaging. Every call from every run is pooled and the aggregate
is recomputed from the pooled calls, so a measure backed by 78 calls is not
diluted by being reported alongside one backed by 36.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .audio import BargeIn, CallMetrics, aggregate, aggregate_by_expectation
from .util import die, info, read_json, write_json


def _calls(analysis: Dict[str, Any]) -> Dict[str, List[CallMetrics]]:
    out: Dict[str, List[CallMetrics]] = {}
    for p in analysis.get("per_call", []):
        d = {k: v for k, v in p["metrics"].items() if k != "segments"}
        d["barge_ins"] = [BargeIn(**b) for b in d.get("barge_ins", [])]
        out.setdefault(p["system"], []).append(CallMetrics(**d))
    return out


def combine(run_dirs: List[Path], out_dir: Path, label: str = "combined",
            group_names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Pool the calls of several runs and recompute everything from the pool."""
    group_names = group_names or {}
    analyses, manifests = [], []
    for rd in run_dirs:
        ap = rd / "analysis.json"
        if not ap.exists():
            die(f"{rd} has no analysis.json - run `earshot ingest` on it first")
        analyses.append(read_json(ap))
        manifests.append(read_json(rd / "manifest.json"))

    systems: List[str] = []
    for a in analyses:
        for s in a["systems"]:
            if s not in systems:
                systems.append(s)

    pooled: Dict[str, List[CallMetrics]] = {s: [] for s in systems}
    per_call: List[Dict[str, Any]] = []
    by_scenario: Dict[str, Any] = {}
    exp_pairs: Dict[str, List] = {s: [] for s in systems}

    for rd, a, m in zip(run_dirs, analyses, manifests):
        gname = group_names.get(rd.name) or m.get("battery", rd.name)
        for sysid, calls in _calls(a).items():
            pooled.setdefault(sysid, []).extend(calls)
        for p in a.get("per_call", []):
            per_call.append(dict(p, run=rd.name, group=gname))
        for sid, sc in (a.get("by_scenario") or {}).items():
            key = sid if sid not in by_scenario else f"{rd.name}:{sid}"
            by_scenario[key] = dict(sc, group=gname, run=rd.name)
            exp = sc.get("barge_expectation", "none")
            for sysid in systems:
                for c in _calls(a).get(sysid, []):
                    pass  # expectation pairing is rebuilt below from per_call

    # Rebuild expectation pairing across the pool, so yield and false-stop rates
    # count every barge-in scenario from every run exactly once.
    for rd, a in zip(run_dirs, analyses):
        exp_by_scn = {sid: sc.get("barge_expectation", "none")
                      for sid, sc in (a.get("by_scenario") or {}).items()}
        for p in a.get("per_call", []):
            d = {k: v for k, v in p["metrics"].items() if k != "segments"}
            d["barge_ins"] = [BargeIn(**b) for b in d.get("barge_ins", [])]
            exp_pairs.setdefault(p["system"], []).append(
                (exp_by_scn.get(p["scenario"], "none"), CallMetrics(**d)))

    measured: Dict[str, Any] = {}
    for s, calls in pooled.items():
        if not calls:
            continue
        measured[s] = aggregate(calls)
        measured[s]["barge_in_by_expectation"] = aggregate_by_expectation(
            exp_pairs.get(s, []))

    out = {
        "run_id": label,
        "systems": systems,
        "sources": [{"run": rd.name, "battery": m.get("battery"),
                     "calls": len(a.get("per_call", [])),
                     "scenarios": len(a.get("by_scenario") or {}),
                     "group": group_names.get(rd.name) or m.get("battery")}
                    for rd, a, m in zip(run_dirs, analyses, manifests)],
        "measured": measured,
        "by_scenario": by_scenario,
        "per_call": per_call,
        "qa": [q for a in analyses for q in a.get("qa", [])],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "analysis.json", out)
    info(f"pooled {len(per_call)} calls from {len(run_dirs)} run(s), "
         f"{len(by_scenario)} scenarios, systems {', '.join(systems)}")
    return out
