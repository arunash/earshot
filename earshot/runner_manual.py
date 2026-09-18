"""Manual mode: you place the calls, Earshot tells you exactly what to say and
then does all the measurement.

This is the default because it needs no telephony account, no public URL, and no
per-minute spend - and because a human tester catches things an automated caller
never will. The cost is that you have to record the calls yourself.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .battery import Battery, Scenario
from .util import bold, cyan, dim, info, warn, yellow

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".mp4", ".caf"}

# <system>-<scenario>-run<n>.<ext>, tolerant about separators and case.
# The scenario is any letter-prefixed id, not just S-numbers: the robustness
# ladder uses N/J/C/D/X prefixes to group its rungs by what they impair.
NAME_RE = re.compile(
    r"^(?P<system>[A-Za-z0-9]+)[-_](?P<scenario>[A-Za-z]{1,3}\d{2,3})"
    r"[-_]?(?:run)?(?P<run>\d+)?$",
    re.IGNORECASE,
)


def call_sheet(battery: Battery, plan: List[Dict[str, Any]],
               manifest: Dict[str, Any]) -> str:
    """The tester's script. Print it, put it on a second screen, work down it."""
    L: List[str] = []
    ids = [s["id"] for s in manifest["systems"]]
    L.append(f"# Call sheet — {manifest['run_id']}")
    L.append("")
    L.append(f"{battery.name} v{battery.version} · {len(plan)} calls · "
             f"systems {', '.join(ids)}")
    L.append("")
    L.append("## Before you start")
    L.append("")
    L.append("- **Record every call in DUAL CHANNEL if you possibly can.** "
             "Mono still gives you latency, dead air and talk ratio, but barge-in "
             "stop latency — the single highest-signal number here — cannot be "
             "measured from a mono mixdown.")
    L.append("- Save each recording as `<SYSTEM>-<SCENARIO>-run<N>.wav` "
             "(e.g. `A-S10-run2.wav`) in the run's `recordings/` folder.")
    L.append("- Same handset, same room, same noise source for every system.")
    L.append("- Write freeform notes in `<SYSTEM>/notes.md` as you go. The judge "
             "reads them.")
    L.append("- The order below is deliberately shuffled per system. Follow it; "
             "do not batch one system's calls together.")
    L.append("")
    if battery.context:
        L.append("## Context")
        L.append("")
        L.append(battery.context)
        L.append("")

    L.append("## Calls")
    L.append("")
    by_scn = {s.id: s for s in battery.scenarios}
    for item in plan:
        sc = by_scn[item["scenario"]]
        num = item["system"]
        L.append(f"### {item['index']:>3}. System {num} · {sc.id} {sc.name} "
                 f"· run {item['run']}")
        L.append("")
        L.append(f"`{num}-{sc.id}-run{item['run']}.wav`")
        L.append("")
        if sc.setup:
            L.append("**Setup:** " + ", ".join(f"{k}: {v}" for k, v in sc.setup.items()))
            L.append("")
        L.append("**Say:**")
        L.append("")
        for t in sc.turns:
            if not t.say:
                continue
            marks = []
            if t.wait == "during_agent":
                marks.append(f"INTERRUPT ~{t.offset_ms/1000:.1f}s into its reply")
            elif t.wait == "fixed":
                marks.append(f"wait {t.offset_ms/1000:.1f}s of SILENCE first")
            if t.rate:
                marks.append(f"speak {t.rate}")
            if t.volume:
                marks.append(t.volume)
            if t.voice != "default":
                marks.append(t.voice.replace("_", " "))
            suffix = f"  _({'; '.join(marks)})_" if marks else ""
            L.append(f"> {t.say}{suffix}")
            L.append("")
        if sc.tester_script:
            L.append("**How to run it:** " + sc.tester_script.replace("\n", " ").strip())
            L.append("")
        if sc.pass_signals or sc.fail_signals:
            L.append("**Watch for:** "
                     + "; ".join(f"good — {p}" for p in sc.pass_signals)
                     + (" | " if sc.pass_signals and sc.fail_signals else "")
                     + "; ".join(f"bad — {f}" for f in sc.fail_signals))
            L.append("")
        L.append("Notes: ______________________________________________")
        L.append("")
    return "\n".join(L)


def discover(run_dir: "str | Path", systems: List[str]) -> List[Dict[str, Any]]:
    """Find recordings under a run directory, in either supported layout."""
    run_dir = Path(run_dir)
    roots = [run_dir / "recordings"] + [run_dir / s for s in systems]
    found: List[Dict[str, Any]] = []
    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.suffix.lower() not in AUDIO_EXT or p.name.startswith("."):
                continue
            if ".norm" in p.name or p.name.startswith("_leg_"):
                continue
            m = NAME_RE.match(p.stem)
            if not m:
                warn(f"skipping {p.name}: expected <SYSTEM>-<SCENARIO>-run<N>.wav")
                continue
            sysid = m.group("system").upper()
            if sysid not in {s.upper() for s in systems}:
                # Per-system folder layout: A/S10-run2.wav has no system prefix,
                # so fall back to the parent directory name.
                if root.name.upper() in {s.upper() for s in systems}:
                    sysid = root.name.upper()
                else:
                    warn(f"skipping {p.name}: system {sysid!r} not in this run")
                    continue
            key = (sysid, m.group("scenario").upper(), m.group("run") or "1")
            if key in seen:
                continue
            seen.add(key)
            found.append({
                "system": sysid,
                "scenario": key[1],
                "run": int(key[2]),
                "path": str(p),
            })
    return sorted(found, key=lambda d: (d["system"], d["scenario"], d["run"]))


def read_notes(run_dir: "str | Path", system: str) -> str:
    for cand in (Path(run_dir) / system / "notes.md",
                 Path(run_dir) / f"{system}-notes.md"):
        if cand.exists():
            return cand.read_text(encoding="utf-8").strip()
    return ""
