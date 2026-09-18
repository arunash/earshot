# earshot

**A blind, reproducible benchmark for production phone voice agents.**

You have two phone numbers and no idea what is behind them. Earshot tells you
which one you should ship — and shows its work.

Most voice-AI comparisons are vibes: someone calls each number twice, listens,
and forms an impression. Earshot replaces the impression with a call battery that
provokes specific failures, a waveform analyzer that measures what actually
happened to within 10ms, and a scored report card that has to cite evidence for
every number it gives.

```
  A  B+   86.4/100
  B  D    62.1/100

  System A wins by 24 points and the margin is almost entirely turn-taking.
  A stops talking 270ms after you interrupt; B keeps going for 2.1 seconds,
  which on a real line reads as the agent ignoring you.
```

---

## Why this exists

Three things decide whether callers tolerate a voice agent, and none of them
appear in a vendor demo:

1. **False barge-in.** Every vendor demos interrupting the agent. Almost nobody
   demos a cough, an "mm-hmm", or a spouse talking in the background — the things
   that make an agent stop dead ten times a call. Earshot measures the *false*
   stop rate separately from the real yield rate, because they are opposite
   defects and a system can be terrible at exactly one of them.

2. **p90 latency, not median.** A system averaging 600ms with occasional 2.5s
   stalls feels broken. A flat 900ms feels calm. Earshot reports the whole
   distribution and the interquartile spread, and weights the tail.

3. **Fluent confabulation.** A voice that sounds wonderful while inventing a
   price is the most expensive failure in the set. It gets flagged CRITICAL
   regardless of how good the call sounded.

## What it measures

Numbers come from the waveform, not from a model:

| Measured | How |
|---|---|
| Turn latency (median, p90, worst, IQR) | Gap from caller speech offset to agent speech onset |
| Barge-in stop latency | Agent speech offset minus caller interruption onset |
| Yield rate on real interrupts | Fraction of interruptions that ended the agent's turn |
| **False-stop rate** | Fraction of backchannels/coughs that ended it — the one nobody tests |
| Times it cut the caller off | Agent onset inside a caller turn with >400ms still to go |
| Agent talk ratio, mean turn length | Long turns are a defect on a phone call |
| Dead air | Gaps where neither party speaks |

Judgment — hearing accuracy, task completion, patience, honesty — is scored by
Claude against the transcripts, the measured numbers, and your tester notes. The
judge is required to cite a scenario ID plus a quote or a number for every score,
and to emit `null` rather than guess.

Run `earshot selftest` to verify the analyzer against a synthetic call with known
ground truth before you trust a report. It should recover every boundary to
within 60ms.

## The battery

18 scenarios, each designed to provoke one failure:

| | | |
|---|---|---|
| **S01** Clean baseline | **S07** Disfluency & self-repair | **S13** Dead air |
| **S02** Background noise | **S08** Pace extremes | **S14** Long monologue |
| **S03** Channel stress | **S09** Code-switching & proper nouns | **S15** Patience under repetition |
| **S04** Accents | **S10** True barge-in | **S16** Empathy |
| **S05** Alphanumerics & spelling | **S11** **False barge-in** | **S17** Frustration & anger |
| **S06** Numbers, dates & money | **S12** Premature endpointing | **S18** Adversarial & honesty |

Presets: `--preset quick` (6 scenarios), `core` (8, covers all seven rubric
dimensions), `full` (all 18).

Everything lives in [`earshot/batteries/default.yaml`](earshot/batteries/default.yaml). Swap the
lines for your own domain — but keep each scenario's *structure*, because the
structure is what the metric measures.

## Scoring

Seven weighted dimensions, 0–5 each, weighted to 100. Re-weight for your
deployment with `--profile`:

| Dimension | default | transactional | conversational | high_stakes |
|---|---:|---:|---:|---:|
| Turn-taking & barge-in | 20 | 18 | 22 | 15 |
| ASR robustness | 18 | 20 | 14 | 20 |
| Latency & jitter | 15 | 14 | 16 | 10 |
| Task completion | 15 | 25 | 10 | 18 |
| Fluidity & prosody | 12 | 7 | 20 | 5 |
| Patience & empathy | 10 | 8 | 12 | 12 |
| Honesty & escalation | 10 | 8 | 6 | 20 |

`earshot rubric --profile transactional` prints the anchors.

---

## Install

```bash
git clone https://github.com/arunash/earshot && cd earshot
python3 -m venv .venv && .venv/bin/pip install -e '.[judge]'
brew install ffmpeg whisper-cpp        # ffmpeg required; whisper optional
.venv/bin/earshot doctor
```

`ffmpeg` is the only hard requirement. Transcription and judging degrade
gracefully: without whisper you can paste your own transcripts, and without an
API key `earshot judge --dry-run` writes the assembled prompt for you to paste
into any Claude session.

## Use it

### 1. Start a blind run

```bash
earshot new "vendor-a=+15551234567" "vendor-b=+15559876543" \
  --preset core --profile transactional
```

Labels are sealed (base64 in the manifest) so you do not read the vendor name off
your own notes while scoring. Systems become `A`, `B`, … and the judge never sees
anything else. Scenario order is shuffled *independently per system*, so tester
fatigue cannot line up with one position in the script.

### 2. Make the calls

Open `runs/<id>/CALL_SHEET.md`. It gives you, per call, the exact lines to say,
where to interrupt, what noise to play, and what to watch for.

> **Record in dual channel.** Mono still gives you latency, dead air and talk
> ratio, but two voices summed into one signal make barge-in stop latency
> unmeasurable — and that is the highest-signal number in the battery. Earshot
> tells you loudly when a recording is mono rather than quietly reporting a
> wrong number.

Save each file as `<SYSTEM>-<SCENARIO>-run<N>.wav` in `runs/<id>/recordings/`,
and write freeform observations in `runs/<id>/<SYSTEM>/notes.md` — the judge
reads them.

**Or let Earshot place the calls** (Twilio, optional):

```bash
earshot doctor --twilio          # account live? which numbers can we call from?
earshot call <run-id> --dry-run  # print the TwiML, call nobody
earshot call <run-id>            # dual-channel recordings, downloaded
```

No tunnel, no server, no public URL: TwiML is delivered inline on the call
itself. (`earshot serve` + `EARSHOT_PUBLIC_URL` remain as a fallback for
scenarios that outgrow Twilio's 4000-character inline limit — the longest
scenario in the default battery renders 729.)

Injection is open-loop — the harness plays its lines on a fixed timeline rather
than reacting in real time. That is sound, because *nothing is measured at
injection time*: the interruption only has to land somewhere inside the agent's
turn, and exactly where it landed is recovered afterwards from the waveform.
`earshot ingest` verifies every barge-in actually landed and names the calls to
re-run if one missed.

### 3. Measure, judge, report

```bash
earshot ingest <run-id>     # waveform metrics + speaker-attributed transcripts
earshot judge  <run-id>     # scored against the rubric, with cited evidence
earshot report <run-id>     # REPORT.md + a standalone report.html
earshot unseal <run-id>     # only now: which letter was which vendor
```

One-off measurement of any recording, no run required:

```bash
earshot analyze call.wav --segments
```

---

## What Earshot will not do

- **It will not tell you a mono recording's barge-in latency.** It reports the
  field as unmeasurable instead of estimating it.
- **It will not average away variance.** Three runs that disagree is a finding,
  not noise — the judge is instructed to report it as a defect.
- **It will not converge two systems toward similar scores.** If one is clearly
  worse the report says so, with the margin.
- **It will not guess the vendor inside a scored section.** There is a blind-guess
  section, written last, marked speculative, scoring nothing.

## Costs

Manual mode: free apart from the calls themselves. Automated mode: Twilio
per-minute. Judging one `core` run of two systems is a single Claude request of
roughly 15–60k input tokens depending on transcript length.

## Layout

```
earshot/batteries/default.yaml   the 18 scenarios
earshot/prompts/judge.md         the judge prompt (read it; the opinionated part)
earshot/audio.py         every measured number, and nothing else
earshot/rubric.py        dimensions, weights, profiles
earshot/selftest.py      calibration against synthetic ground truth
runs/<id>/               manifest, recordings, metrics, transcripts, report
```

`runs/` is gitignored. Call recordings contain real voices — do not commit them.

## License

MIT. See [docs/METRICS.md](docs/METRICS.md) for exactly how each number is
derived, and [CONTRIBUTING.md](CONTRIBUTING.md) to add a scenario.
