---
name: voice-agent-benchmark
description: Blind, reproducible benchmarking of production phone voice agents with earshot. It places real test calls via Twilio, measures turn latency, barge-in, false triggers and dead air from dual-channel waveforms, and scores hearing, task, empathy and honesty with cited evidence. It can also re-test a tuned agent against a frozen baseline and produce a before/after comparison PDF. Use when asked to test, benchmark, compare or re-test a voice agent, IVR or AI phone line, or to check whether tuning made one better.
---

# Voice agent benchmark (earshot)

Earshot answers "which phone agent should we ship, and did the last change help?" from evidence rather than impressions. It calls each number with a fixed battery of scripted callers, measures what happened from the waveform, then scores the transcripts against a rubric that requires citations.

Repo: https://github.com/arunash/earshot (MIT)

## The one rule
**Measured numbers and judged numbers stay separate.**

- **Measured** (`earshot/audio.py`, cannot be flattered by a model):
  - turn latency: median, p90, IQR
  - barge-in stop latency and yield rate
  - false triggers
  - cut-offs, dead air, talk ratio
- **Judged:** seven 0–5 dimensions, weighted to 100 (`earshot rubric`):
  - turn-taking
  - ASR robustness
  - latency
  - task completion
  - fluidity
  - patience/empathy
  - honesty

Every judged score must cite a scenario ID plus a quote or a number. Emit `null` rather than guess.

## Before anything else, ask the user for
- The phone numbers under test, and a label for each. Labels are sealed in the manifest; refer to systems only as A/B/C until `earshot unseal`.
- The Twilio caller number they own (`TWILIO_FROM_NUMBER`). Use only that number. Many agents authenticate the caller ID, so changing it changes the test.
- Which battery to run (see `earshot/batteries/`), and approval of the spend: roughly 1–1.5 min of Twilio time per call. **Confirm before placing more than ~100 calls.**

## Setup
```bash
git clone https://github.com/arunash/earshot && cd earshot
python3 -m venv .venv && .venv/bin/pip install -e '.[judge,twilio]'
brew install ffmpeg whisper-cpp
set -a; source .env; set +a        # TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER — never commit
.venv/bin/earshot doctor --twilio
.venv/bin/earshot selftest         # must recover known boundaries within 60ms
```

## Fresh benchmark
```bash
earshot new "vendor-a=+1555..." "vendor-b=+1555..." --battery <battery> --preset full
earshot bake <run> --publish       # only for robustness batteries (noise/codec/loss audio)
earshot call <run> --pause 10      # dual-channel recordings, downloaded
earshot ingest <run>               # waveform metrics + whisper transcripts
earshot judge <run> [--dry-run]    # --dry-run writes judge-prompt.md to score in-session
earshot report <run>               # REPORT.md + report.html
```

Run a robustness ladder (one script under ~21 channel conditions) **and** a scenario battery (~18 scenarios, one per failure mode). Each alone misleads: the ladder makes systems look identical, and the battery alone confuses hearing problems with conversation problems. Pool them with `earshot combine <name> <ladder-run> <battery-run>`. Judge each part separately, then merge the results.

## Line health: check before and during every run
- **Probe first.** Place 2 calls per line (filter the manifest's `plan`) and confirm both are full length and the agent actually speaks.
- **Know the two failure signatures.** Twilio reports both as `completed`:
  - **Ringback only:** 15–35s calls, and the transcript is "(phone ringing)" with no agent speech. The line isn't answering.
  - **Answer then hang-up:** 5–18s calls, and the call ends after the greeting. The agent backend is down.
- **Guard during the run.** Flag any recording much shorter than a healthy call. Two in a row on one line means stop that line; don't burn the battery on it.
- **Recover.** `earshot call` skips recordings that already exist. Delete the short ones, restrict `plan` to the healthy system, and resume later.
- **Never ingest or score dead calls.** They read as a quality collapse.

## Re-testing after tuning (before/after)
1. **Freeze the conditions.** Create the new run by *cloning the old run's manifest*, not with `earshot new`. Copy `manifest.json`, `audio/` and `CALL_SHEET.md`, then change `run_id`. That keeps the plan, call order, baked audio and hosted URLs identical, so any delta is the system, not the test.
2. **Freeze the baseline.** Build a combined run containing:
   - the old systems' recordings, `metrics/` and `transcripts/`, copied with `cp -p` (timestamps preserved, so ingest reuses them as cached)
   - the re-tested line as a new system ID (e.g. `C` = "B after tuning"), added to `systems` and `plan`
3. **Verify the freeze.** After ingest, the old systems' pooled metrics must match the old report exactly.
4. **Score only the new system.** Carry the old systems' dimension scores over verbatim, and score the new one anchored against the old transcripts of the same scenarios.
5. **Untestable systems.** If one line can't be re-called, carry its numbers over and say so prominently.

## Reporting a comparison
Produce a two-page PDF (HTML → headless Chrome `--print-to-pdf`; check the page count before delivering).

- **Page 1:**
  - a one-paragraph verdict
  - score cards (before, after, reference)
  - numbered differences tagged **WORSE / BETTER / UNCHANGED**, each backed by a measured number or a verbatim quote
- **Page 2:**
  - the measured before/after table, with the reference system's column
  - the score by dimension, with what drove each change
  - what to ask the vendor to fix
  - caveats

Use plain names ("Line B before/after tuning") rather than run letters, so the reader needs no context.

## Honesty rules for the write-up
- **Lead with measured deltas.** Judged deltas come second.
- **State the sample.** Usually there is one run per scenario. Single-call quotes are illustrative; pooled metrics are the firmer evidence.
- **Correct the record.** If re-reading old transcripts shows the earlier report was wrong, say so, but keep the old score fixed so the baseline doesn't move.
- **Don't shrink gaps.** Don't average away disagreement, and don't converge systems toward similar scores. If one is clearly worse, say by how much.
- **Respect mono recordings.** Barge-in from a mono recording is unmeasurable; report it that way rather than estimating.
