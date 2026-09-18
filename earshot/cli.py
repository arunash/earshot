from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, battery as battery_mod, judge as judge_mod, report, rubric
from .audio import CallMetrics, aggregate, aggregate_by_expectation, analyze
from .runner_manual import call_sheet, discover, read_notes
from .util import (bold, cyan, die, dim, green, have, info, read_json, red,
                   data_dir, warn, write_json, yellow)

PRESETS = {
    "quick": ["S01", "S05", "S10", "S11", "S15", "S18"],
    "core":  ["S01", "S05", "S10", "S11", "S12", "S14", "S15", "S18"],
    "full":  None,
}


def _runs_root() -> Path:
    return Path(os.environ.get("EARSHOT_RUNS", "runs"))


def _run_dir(run_id: str) -> Path:
    p = Path(run_id)
    if p.is_dir() and (p / "manifest.json").exists():
        return p
    p = _runs_root() / run_id
    if not (p / "manifest.json").exists():
        die(f"no run {run_id!r} (looked in {p}). `earshot new` creates one.")
    return p


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "run"


# ------------------------------------------------------------------- doctor --


def cmd_doctor(a) -> int:
    from . import transcribe as tr
    rows = []

    def row(name, ok, detail):
        rows.append((name, ok, detail))

    row("ffmpeg", have("ffmpeg"), "required — normalizes recordings")
    row("ffprobe", have("ffprobe"), "required — detects mono vs stereo")
    eng = tr.available("auto")
    row("transcription", eng is not None,
        f"{eng}" if eng else "install whisper-cpp or faster-whisper, "
                             "or paste transcripts yourself")
    key = bool(os.environ.get("ANTHROPIC_API_KEY") or
               os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    try:
        import anthropic  # noqa: F401
        sdk = True
    except ImportError:
        sdk = False
    row("judge (anthropic sdk)", sdk, "pip install 'earshot[judge]'")
    row("judge (credentials)", key,
        "ANTHROPIC_API_KEY — or use `earshot judge --dry-run` and paste the "
        "prompt into any Claude session")
    tw = bool(os.environ.get("TWILIO_ACCOUNT_SID") and
              os.environ.get("TWILIO_AUTH_TOKEN"))
    row("twilio (automated mode)", tw, "optional — manual mode needs none of it")
    pub = os.environ.get("EARSHOT_PUBLIC_URL", "")
    row("public URL (automated mode)", pub.startswith("https://"),
        "optional — needed only for `earshot call`")

    print(bold("earshot doctor"))
    print()
    required_ok = True
    for name, ok, detail in rows:
        mark = green("ok  ") if ok else yellow("miss")
        print(f"  {mark}  {name:<28} {dim(detail)}")
        if not ok and name in ("ffmpeg", "ffprobe"):
            required_ok = False
    print()
    if required_ok:
        print(green("ready to measure calls"))
    else:
        print(red("install ffmpeg before running anything else"))
    if not (eng and sdk):
        print(dim("  Missing pieces degrade gracefully: metrics always work, and "
                  "`judge --dry-run` writes a prompt you can paste anywhere."))

    if a.twilio:
        print()
        _check_twilio()
    elif tw:
        print(dim("  `earshot doctor --twilio` verifies the account is live and "
                  "lists numbers you can call from."))
    return 0 if required_ok else 1


def _check_twilio() -> None:
    """Live check: is the account usable, and what can we call from?

    Worth doing before a run rather than discovering a suspended account after
    the first call fails.
    """
    print(bold("twilio"))
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TWILIO_AUTH_TOKEN")
    if not (sid and tok):
        print(f"  {yellow('miss')}  credentials — set TWILIO_ACCOUNT_SID and "
              f"TWILIO_AUTH_TOKEN")
        return
    try:
        from twilio.rest import Client
    except ImportError:
        print(f"  {yellow('miss')}  twilio sdk — pip install 'earshot[twilio]'")
        return
    try:
        client = Client(sid, tok)
        acct = client.api.accounts(sid).fetch()
        ok = acct.status == "active"
        mark = green("ok  ") if ok else red("bad ")
        print(f"  {mark}  account {acct.friendly_name!r} — status {acct.status}")
        if not ok:
            print(red("        the account is not active; outbound calls will fail"))
        bal = client.api.accounts(sid).balance.fetch()
        print(f"  {dim('    ')}  balance {bal.balance} {bal.currency}")
        nums = client.incoming_phone_numbers.list(limit=20)
        if nums:
            print(f"  {green('ok  ')}  {len(nums)} number(s) you can call from:")
            for n in nums:
                print(f"        {n.phone_number}  {dim(n.friendly_name or '')}")
            print(dim("        set TWILIO_FROM_NUMBER to one of these"))
        else:
            print(f"  {yellow('miss')}  no phone numbers on this account")
    except Exception as e:
        print(f"  {red('bad ')}  {type(e).__name__}: {str(e)[:160]}")


def cmd_selftest(a) -> int:
    from .selftest import run
    return 0 if run() else 1


# ---------------------------------------------------------------------- new --


def cmd_new(a) -> int:
    b = battery_mod.load(a.battery)
    systems: List[Dict[str, Any]] = []
    letters = [chr(ord("A") + i) for i in range(26)]

    entries = []
    for i, spec in enumerate(a.systems):
        if "=" in spec:
            label, phone = spec.split("=", 1)
        else:
            label, phone = f"system{i+1}", spec
        entries.append((label.strip(), phone.strip()))

    if a.shuffle_letters:
        random.Random(a.seed).shuffle(entries)
    for i, (label, phone) in enumerate(entries):
        systems.append({
            "id": letters[i],
            "phone": phone,
            # base64, not encryption — just enough that you do not read the
            # vendor name off the manifest while you are scoring the calls.
            "sealed_label": base64.b64encode(label.encode()).decode(),
        })

    only = PRESETS.get(a.preset)
    if a.only:
        only = [s.strip().upper() for s in a.only.split(",")]
    skip = [s.strip().upper() for s in a.skip.split(",")] if a.skip else None

    runs = a.runs if a.runs else (1 if a.preset in ("quick",) else b.runs_per_scenario)
    plan = b.plan([s["id"] for s in systems], only=only, skip=skip,
                  runs=runs, shuffle=not a.no_shuffle, seed=a.seed)

    run_id = a.name or f"{_slug(b.id)}-{datetime.now().strftime('%Y%m%d-%H%M')}"
    rd = _runs_root() / run_id
    if rd.exists() and not a.force:
        die(f"{rd} already exists (use --force to overwrite)")
    (rd / "recordings").mkdir(parents=True, exist_ok=True)

    manifest = {
        "run_id": run_id,
        "created": datetime.now().isoformat(timespec="seconds"),
        "earshot_version": __version__,
        "battery": b.id,
        "battery_name": f"{b.name} v{b.version}",
        "profile": a.profile,
        "preset": a.preset,
        "runs_per_scenario": runs,
        "agent_channel": a.agent_channel,
        "systems": systems,
        "plan": plan,
    }
    write_json(rd / "manifest.json", manifest)

    sheet = call_sheet(b, plan, manifest)
    (rd / "CALL_SHEET.md").write_text(sheet, encoding="utf-8")
    for s in systems:
        d = rd / s["id"]
        d.mkdir(exist_ok=True)
        notes = d / "notes.md"
        if not notes.exists():
            notes.write_text(
                f"# Tester notes — system {s['id']}\n\n"
                "Freeform. The judge reads this. Write what you noticed that the\n"
                "numbers will not show: tone, awkwardness, moments you winced.\n\n",
                encoding="utf-8")

    print(bold(f"created {rd}"))
    print()
    print(f"  systems     {', '.join(s['id'] for s in systems)}  "
          f"({len(systems)} numbers, sealed)")
    print(f"  battery     {b.name} v{b.version}, preset {a.preset}, "
          f"{runs} run(s) per scenario")
    print(f"  calls       {len(plan)}")
    print(f"  profile     {a.profile}")
    print()
    print("next:")
    print(f"  1. open {cyan(str(rd / 'CALL_SHEET.md'))} and work down it")
    print(f"  2. drop recordings into {cyan(str(rd / 'recordings'))} "
          f"as {dim('<SYSTEM>-<SCENARIO>-run<N>.wav')}")
    print(f"  3. {cyan('earshot ingest ' + run_id)}")
    print(f"  4. {cyan('earshot judge ' + run_id)} && "
          f"{cyan('earshot report ' + run_id)}")
    print()
    print(dim("  Record in DUAL CHANNEL if you can — mono cannot measure barge-in."))
    return 0


def cmd_sheet(a) -> int:
    rd = _run_dir(a.run)
    print((rd / "CALL_SHEET.md").read_text(encoding="utf-8"))
    return 0


def cmd_unseal(a) -> int:
    rd = _run_dir(a.run)
    m = read_json(rd / "manifest.json")
    print(bold(f"unsealing {m['run_id']}"))
    print()
    for s in m["systems"]:
        label = base64.b64decode(s["sealed_label"]).decode()
        print(f"  {bold(s['id'])}  {label:<24} {dim(s.get('phone', ''))}")
    print()
    print(dim("  Do this AFTER the report is written, not before."))
    return 0


# ------------------------------------------------------------------ analyze --


def cmd_analyze(a) -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        m = analyze(a.recording, td, agent_channel=a.agent_channel,
                    agent_first=not a.caller_first)
    if a.json:
        print(json.dumps(m.to_dict(include_segments=a.segments), indent=2))
        return 0
    print(bold(f"{Path(a.recording).name}  {m.duration_s:.1f}s  "
               f"{'stereo' if m.stereo else 'MONO'}"))
    print()
    print(f"  turns              {m.n_caller_turns} caller / {m.n_agent_turns} agent")
    print(f"  response latency   median {m.latency_median_ms or '—'} ms   "
          f"p90 {m.latency_p90_ms or '—'} ms   worst {m.latency_max_ms or '—'} ms")
    print(f"  latency spread     IQR {m.latency_iqr_ms or '—'} ms")
    if m.barge_in_measurable:
        print(f"  barge-ins          {len(m.barge_ins)} detected, "
              f"stop median {m.barge_in_stop_median_ms or '—'} ms, "
              f"yield rate {m.barge_in_yield_rate if m.barge_in_yield_rate is not None else '—'}")
        for b in m.barge_ins:
            mark = green("yielded") if b.yielded else red("DID NOT YIELD")
            print(f"      at {b.at:6.2f}s  kept talking {b.stop_ms:6.0f} ms  {mark}")
        print(f"  false stops        {m.backchannel_stops} "
              f"(stopped for a sub-1s utterance)")
        print(f"  cut the caller off {m.agent_interruptions} time(s)")
        print(f"  overlap            {m.overlap_s:.2f}s")
    else:
        print(yellow("  barge-in           not measurable on a mono recording"))
    print(f"  agent talk ratio   {m.agent_talk_ratio}")
    print(f"  mean agent turn    {m.mean_agent_turn_s} s "
          f"(longest {m.longest_agent_turn_s} s)")
    print(f"  dead air           {len(m.dead_air_events)} event(s), "
          f"longest {m.longest_dead_air_s or 0} s")
    for n in m.notes:
        print()
        print(yellow("  " + n))
    if a.segments:
        print()
        for s in m.segments:
            print(f"    {s.speaker:6s} {s.start:7.2f} -> {s.end:7.2f}")
    return 0


# ------------------------------------------------------------------- ingest --


def cmd_ingest(a) -> int:
    from . import transcribe as tr
    rd = _run_dir(a.run)
    m = read_json(rd / "manifest.json")
    b = battery_mod.load(m["battery"])
    systems = [s["id"] for s in m["systems"]]
    agent_ch = m.get("agent_channel", 1)

    found = discover(rd, systems)
    if not found:
        die(f"no recordings found under {rd}. Expected files named "
            f"<SYSTEM>-<SCENARIO>-run<N>.wav in {rd / 'recordings'}")

    engine = tr.available(a.transcriber)
    info(f"{len(found)} recording(s); transcriber: {engine or 'none'}")

    work = rd / ".work"
    work.mkdir(exist_ok=True)
    qa: List[str] = []

    for i, rec in enumerate(found, 1):
        stem = f"{rec['system']}-{rec['scenario']}-run{rec['run']}"
        mpath = rd / "metrics" / f"{stem}.json"
        tpath = rd / "transcripts" / f"{stem}.json"

        if mpath.exists() and not a.force:
            cm_dict = read_json(mpath)
            info(f"[{i}/{len(found)}] {stem} cached")
        else:
            info(f"[{i}/{len(found)}] analyzing {stem}")
            cm = analyze(rec["path"], work, agent_channel=agent_ch)
            cm_dict = cm.to_dict(include_segments=True)
            write_json(mpath, cm_dict)

        if not (tpath.exists() and not a.force) and engine:
            info(f"[{i}/{len(found)}] transcribing {stem}")
            segs = None
            if not cm_dict.get("stereo"):
                from .audio import Segment
                segs = [Segment(**s) for s in cm_dict.get("segments", [])]
            t = tr.transcribe_call(rec["path"], work, agent_channel=agent_ch,
                                   engine=a.transcriber, segments=segs)
            write_json(tpath, t)

        # --- QA: did the scenario actually produce what it was designed to? ---
        sc = b.get(rec["scenario"])
        n_barge = len(cm_dict.get("barge_ins", []))
        if sc.barge_expectation in ("yield", "hold"):
            if not cm_dict.get("barge_in_measurable"):
                qa.append(f"{stem}: mono recording — {sc.id} is a barge-in scenario "
                          f"and cannot be scored from it. Re-record in dual channel.")
            elif n_barge == 0:
                qa.append(f"{stem}: the interruption never landed inside an agent "
                          f"turn (0 barge-ins detected). Re-run this call; the "
                          f"agent probably was not talking when we spoke.")
        if cm_dict.get("n_agent_turns", 0) == 0:
            qa.append(f"{stem}: no agent speech detected at all — check the channel "
                      f"mapping (--agent-channel) or whether the call connected.")
        if cm_dict.get("duration_s", 0) < 10:
            qa.append(f"{stem}: only {cm_dict['duration_s']:.0f}s long; likely a "
                      f"failed or abandoned call.")

    # Reload from disk so cached and freshly-analyzed calls aggregate identically.
    from .audio import BargeIn
    exp_by_scn = {sc.id: sc.barge_expectation for sc in b.scenarios}
    per_call: List[Dict[str, Any]] = []
    by_system: Dict[str, List[CallMetrics]] = {s: [] for s in systems}
    pairs_by_system: Dict[str, List] = {s: [] for s in systems}
    for rec in found:
        stem = f"{rec['system']}-{rec['scenario']}-run{rec['run']}"
        raw = read_json(rd / "metrics" / f"{stem}.json")
        flat = {k: v for k, v in raw.items() if k != "segments"}
        cm = CallMetrics(**dict(flat, barge_ins=[BargeIn(**x)
                                                for x in flat.get("barge_ins", [])]))
        by_system[rec["system"]].append(cm)
        pairs_by_system[rec["system"]].append(
            (exp_by_scn.get(rec["scenario"], "none"), cm))
        per_call.append({"system": rec["system"], "scenario": rec["scenario"],
                         "run": rec["run"], "metrics": flat})

    measured = {}
    for s, c in by_system.items():
        if not c:
            continue
        measured[s] = aggregate(c)
        measured[s]["barge_in_by_expectation"] = aggregate_by_expectation(
            pairs_by_system[s])
    analysis = {
        "run_id": m["run_id"],
        "systems": systems,
        "measured": measured,
        "qa": qa,
        "per_call": per_call,
    }
    write_json(rd / "analysis.json", analysis)

    print()
    print(bold("measured"))
    print()
    hdr = f"  {'':<28}" + "".join(f"{s:>12}" for s in systems)
    print(dim(hdr))
    def line(label, fn):
        print(f"  {label:<28}" + "".join(
            f"{fn(measured.get(s, {})):>12}" for s in systems))
    line("turn latency median", lambda x: _fmt(x.get("latency", {}).get("median_ms"), "ms"))
    line("turn latency p90", lambda x: _fmt(x.get("latency", {}).get("p90_ms"), "ms"))
    line("latency IQR (jitter)", lambda x: _fmt(x.get("latency", {}).get("iqr_ms"), "ms"))
    line("barge-in stop median", lambda x: _fmt(x.get("barge_in", {}).get("stop_median_ms"), "ms"))
    line("barge-in yield rate",
         lambda x: _fmt(x.get("barge_in_by_expectation", {})
                        .get("yield", {}).get("yield_rate")))
    line("false-stop rate (S11)",
         lambda x: _fmt(x.get("barge_in_by_expectation", {})
                        .get("hold", {}).get("false_stop_rate")))
    line("cut caller off", lambda x: str(x.get("agent_interruptions", "—")))
    line("agent talk ratio", lambda x: _fmt(x.get("agent_talk_ratio")))
    line("mean agent turn", lambda x: _fmt(x.get("mean_agent_turn_s"), "s"))
    line("calls", lambda x: str(x.get("calls", 0)))

    if qa:
        print()
        print(yellow(bold(f"{len(qa)} QA issue(s) — fix these before judging:")))
        for q in qa:
            print(yellow(f"  - {q}"))
    print()
    print(f"wrote {cyan(str(rd / 'analysis.json'))}")
    print(f"next: {cyan('earshot judge ' + m['run_id'])}")
    return 0


def _fmt(v, unit="") -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.0f}{unit}" if abs(v) >= 10 else f"{v:.2f}{unit}"
    return f"{v}{unit}"


# -------------------------------------------------------------------- judge --


def _build_payload(rd: Path, m: Dict[str, Any]) -> Dict[str, Any]:
    analysis = read_json(rd / "analysis.json")
    b = battery_mod.load(m["battery"])
    used = sorted({p["scenario"] for p in analysis["per_call"]})
    chunks: List[str] = []
    for sysid in analysis["systems"]:
        chunks.append(f"\n## SYSTEM {sysid}\n")
        notes = read_notes(rd, sysid)
        chunks.append(f"### Tester notes for {sysid}\n\n"
                      + (notes or "_(none written)_") + "\n")
        for p in sorted([x for x in analysis["per_call"] if x["system"] == sysid],
                        key=lambda x: (x["scenario"], x["run"])):
            stem = f"{sysid}-{p['scenario']}-run{p['run']}"
            tp = rd / "transcripts" / f"{stem}.json"
            chunks.append(f"### {stem}")
            mm = p["metrics"]
            chunks.append(
                f"_measured: latency median {mm.get('latency_median_ms')}ms, "
                f"p90 {mm.get('latency_p90_ms')}ms; "
                f"{len(mm.get('barge_ins', []))} barge-in(s); "
                f"agent talk ratio {mm.get('agent_talk_ratio')}; "
                f"mean agent turn {mm.get('mean_agent_turn_s')}s_")
            if tp.exists():
                t = read_json(tp)
                chunks.append("```\n" + (t.get("text") or "(empty)") + "\n```")
                if t.get("note"):
                    chunks.append(f"_{t['note']}_")
            else:
                chunks.append("_(no transcript available for this call)_")
            chunks.append("")
    return {
        "battery": {
            "id": b.id, "name": b.name, "version": b.version,
            "context": b.context,
            "scenarios": [s.summary() for s in b.scenarios if s.id in used],
        },
        "measured": analysis["measured"],
        "qa": analysis.get("qa", []),
        "transcripts": "\n".join(chunks),
    }


def cmd_judge(a) -> int:
    rd = _run_dir(a.run)
    m = read_json(rd / "manifest.json")
    if not (rd / "analysis.json").exists():
        die(f"run `earshot ingest {m['run_id']}` first")
    payload = _build_payload(rd, m)
    profile = a.profile or m.get("profile", "default")

    prompt = judge_mod.build_prompt(payload, profile)
    (rd / "judge-prompt.md").write_text(prompt, encoding="utf-8")
    approx = len(prompt) // 4
    info(f"prompt assembled: {len(prompt):,} chars (~{approx:,} tokens) -> "
         f"{rd / 'judge-prompt.md'}")

    if a.dry_run:
        print()
        print(bold("dry run — nothing was sent."))
        print(f"Paste {cyan(str(rd / 'judge-prompt.md'))} into any Claude session, "
              f"save the JSON it returns as {cyan(str(rd / 'judge-result.json'))}, "
              f"then run {cyan('earshot report ' + m['run_id'])}.")
        return 0

    result = judge_mod.judge(payload, profile=profile, model=a.model,
                             effort=a.effort)
    result = judge_mod.apply_scores(result, profile)
    write_json(rd / "judge-result.json", result)

    print()
    for s in result["systems"]:
        print(f"  {bold(s['id'])}  {s['grade']:<3} {s['total']:5.1f}/100   "
              f"{dim(s.get('sketch', '')[:70])}")
    print()
    print(f"next: {cyan('earshot report ' + m['run_id'])}")
    return 0


# ------------------------------------------------------------------- report --


def cmd_report(a) -> int:
    rd = _run_dir(a.run)
    m = read_json(rd / "manifest.json")
    rp = rd / "judge-result.json"
    if not rp.exists():
        die(f"no judge-result.json in {rd}. Run `earshot judge {m['run_id']}` "
            f"(or --dry-run and paste the result there).")
    result = read_json(rp)
    if "systems" in result and not result["systems"][0].get("total"):
        result = judge_mod.apply_scores(result, m.get("profile", "default"))
        write_json(rp, result)
    measured = read_json(rd / "analysis.json")["measured"]

    md = report.markdown(result, measured, m)
    (rd / "REPORT.md").write_text(md, encoding="utf-8")
    hp = rd / "report.html"
    hp.write_text(
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        + report.html_report(result, measured, m), encoding="utf-8")

    if a.stdout:
        print(md)
    else:
        print(bold("report written"))
        print(f"  {cyan(str(rd / 'REPORT.md'))}")
        print(f"  {cyan(str(hp))}")
        print()
        print(result.get("verdict", ""))
        print()
        for s in result["systems"]:
            print(f"  {bold(s['id'])}  {s['grade']:<3} {s['total']:5.1f}/100")
        print()
        print(dim(f"  reveal which system was which: earshot unseal {m['run_id']}"))
    return 0


# --------------------------------------------------------- automated calling --


def cmd_serve(a) -> int:
    from .runner_twilio import serve
    serve(battery_mod.load(a.battery), host=a.host, port=a.port,
          agent_budget_s=a.budget)
    return 0


def cmd_call(a) -> int:
    from .runner_twilio import place_calls
    rd = _run_dir(a.run)
    m = read_json(rd / "manifest.json")
    b = battery_mod.load(m["battery"])
    plan = m["plan"]
    if a.only:
        want = {s.strip().upper() for s in a.only.split(",")}
        plan = [p for p in plan if p["scenario"].upper() in want]
    if a.limit:
        plan = plan[:a.limit]
    results = place_calls(m, plan, rd, b, budget_s=a.budget,
                          pause_between_s=a.pause, dry_run=a.dry_run)
    if results:
        write_json(rd / "call-log.json", results)
        ok = sum(1 for r in results if r.get("recording"))
        print()
        print(f"{ok}/{len(results)} calls recorded -> {rd / 'recordings'}")
        print(f"next: {cyan('earshot ingest ' + m['run_id'])}")
    return 0


def cmd_rubric(a) -> int:
    print(rubric.rubric_markdown(a.profile))
    return 0


# --------------------------------------------------------------------- main --


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="earshot",
        description="Blind, reproducible benchmark for production phone voice agents.")
    p.add_argument("--version", action="version", version=f"earshot {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("doctor", help="check that the toolchain is ready")
    s.add_argument("--twilio", action="store_true",
                   help="also verify the Twilio account is live and list numbers")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("selftest", help="verify the analyzer against known ground truth")
    s.set_defaults(func=cmd_selftest)

    s = sub.add_parser("rubric", help="print the scoring rubric")
    s.add_argument("--profile", default="default", choices=sorted(rubric.PROFILES))
    s.set_defaults(func=cmd_rubric)

    s = sub.add_parser("new", help="start a blind run")
    s.add_argument("systems", nargs="+",
                   help="phone numbers, optionally label=number "
                        "(labels are sealed, not shown while you score)")
    s.add_argument("--name", help="run id (default: battery + timestamp)")
    s.add_argument("--battery", default="default")
    s.add_argument("--preset", default="core", choices=sorted(PRESETS))
    s.add_argument("--profile", default="default", choices=sorted(rubric.PROFILES))
    s.add_argument("--runs", type=int, help="runs per scenario")
    s.add_argument("--only", help="comma-separated scenario ids")
    s.add_argument("--skip", help="comma-separated scenario ids")
    s.add_argument("--agent-channel", type=int, default=1,
                   help="which channel of a dual recording is the agent (default 1)")
    s.add_argument("--seed", type=int, default=None)
    s.add_argument("--no-shuffle", action="store_true",
                   help="keep scenario order fixed (not recommended)")
    s.add_argument("--shuffle-letters", action="store_true",
                   help="randomize which number gets which letter")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("sheet", help="print a run's call sheet")
    s.add_argument("run")
    s.set_defaults(func=cmd_sheet)

    s = sub.add_parser("analyze", help="measure one recording, no run needed")
    s.add_argument("recording")
    s.add_argument("--agent-channel", type=int, default=1)
    s.add_argument("--caller-first", action="store_true",
                   help="mono only: the caller speaks first, not the agent")
    s.add_argument("--segments", action="store_true", help="print every segment")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("ingest", help="measure + transcribe a run's recordings")
    s.add_argument("run")
    s.add_argument("--transcriber", default=os.environ.get("EARSHOT_TRANSCRIBER", "auto"),
                   choices=["auto", "whisper-cpp", "faster-whisper", "none"])
    s.add_argument("--force", action="store_true", help="re-analyze cached calls")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("judge", help="score a run with Claude")
    s.add_argument("run")
    s.add_argument("--profile", choices=sorted(rubric.PROFILES))
    s.add_argument("--model", default=judge_mod.MODEL)
    s.add_argument("--effort", default="high",
                   choices=["low", "medium", "high", "xhigh", "max"])
    s.add_argument("--dry-run", action="store_true",
                   help="assemble the prompt but send nothing")
    s.set_defaults(func=cmd_judge)

    s = sub.add_parser("report", help="render the report card")
    s.add_argument("run")
    s.add_argument("--stdout", action="store_true")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("unseal", help="reveal which system was which")
    s.add_argument("run")
    s.set_defaults(func=cmd_unseal)

    s = sub.add_parser("serve", help="TwiML server for automated calling")
    s.add_argument("--battery", default="default")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--budget", type=float, default=6.0,
                   help="assumed agent turn length in seconds")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("call", help="place a run's calls via Twilio")
    s.add_argument("run")
    s.add_argument("--only", help="comma-separated scenario ids")
    s.add_argument("--limit", type=int)
    s.add_argument("--budget", type=float, default=6.0)
    s.add_argument("--pause", type=float, default=20.0,
                   help="seconds between calls")
    s.add_argument("--dry-run", action="store_true", help="print TwiML, call nobody")
    s.set_defaults(func=cmd_call)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
