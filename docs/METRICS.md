# How every number is derived

Nothing in this file involves a model. These are waveform measurements, and the
whole point of separating them from the judge is that they cannot be flattered.

## Voice activity detection

`earshot/audio.py`. Frames of 20ms at a 10ms hop; RMS converted to dBFS.

- **Noise floor** = 20th percentile of frame energies for that channel.
- **Threshold** = floor + 10dB, clamped to sit at least 4dB below the 97th
  percentile so a very hot or very quiet line still gates sensibly.
- Runs of above-threshold frames are grouped, joining gaps shorter than 200ms.
- Runs shorter than 120ms are discarded as clicks.

**A segment's end is the last frame genuinely above threshold**, not the end of
the merge window. This matters: if the merge gap extended segment ends, every
barge-in stop latency would be inflated by up to 200ms, uniformly, and silently.

All thresholds are in `DEFAULTS` and overridable.

## Speaker attribution

**Dual channel (preferred).** Each leg is its own channel, so attribution is
exact and overlap is directly observable. Twilio's `recording_channels="dual"`
puts the outbound leg on channel 0 and the answering party on channel 1; that is
the `--agent-channel 1` default.

**Mono (degraded).** Segments are clustered into two speakers by a 16-band log
spectral signature and 2-means. The cluster owning the first segment is assumed
to be the agent, since the agent answers the phone. A separation score below 1.2
is flagged as unreliable.

On mono, **overlap cannot be observed at all** — the two voices sum into one
signal, so a barge-in looks like one continuous run of speech. Earshot marks
`barge_in_measurable: false` and reports the field as unavailable rather than
producing a number that looks real.

## Response latency

For each caller segment, the first agent segment beginning at or after the caller
stopped, provided no other caller segment intervenes and the agent was not
already speaking. Latency is the gap between them, in milliseconds.

Reported as median, p90, worst, and **interquartile range**. The IQR is the
jitter number that matters: a system with a 600ms median and a 900ms IQR feels
worse than one with a 900ms median and a 150ms IQR, because callers calibrate to
a rhythm and notice the breaks in it.

Gaps over 15s are dropped as call-structure artifacts rather than turn latency.

## Barge-in

An event is a caller segment whose onset falls strictly inside an agent segment.

- **Stop latency** = agent segment end − caller onset. This is how long it kept
  talking after you started.
- **Yielded** = stop latency ≤ 1200ms. Past about a second of being talked over,
  callers conclude the agent never heard them, so that is the line between
  "yielded slowly" and "did not yield".

Events are then split by what the scenario was *designed* to provoke:

| `barge_expectation` | Scenario | Every event is… | Reported as |
|---|---|---|---|
| `yield` | S10 | a genuine interruption | yield rate, stop latency |
| `hold` | S11 | a cough, backchannel, or third party | **false-stop rate** |

This split is the reason Earshot exists. Yield rate and false-stop rate are
opposite defects measured on the same mechanism, and a system can be excellent at
one and catastrophic at the other. Reporting a single "barge-in score" hides
exactly the failure your callers will complain about.

## Caller cut off

An agent segment beginning inside a caller segment that still had more than 400ms
to run. The 400ms floor keeps natural turn-boundary overlap from counting as an
interruption.

## Dead air, talk ratio, turn length

- **Dead air**: any gap over 3s in which neither party is speaking.
- **Agent talk ratio**: agent speech seconds over total speech seconds. On a
  transactional line anything much above 0.5 means it is monologuing.
- **Mean / longest agent turn**: long turns are a defect on a phone call even
  when every word is correct, because they are what the caller has to interrupt.

## Calibration

`earshot selftest` synthesizes a call with known turn boundaries — including a
barge-in at a known offset with a known stop latency, and a known dead-air gap —
then asserts the analyzer recovers each within 60ms (0.15s for second-scale
values). Observed error is typically 20ms, dominated by the onset ramp of the
synthetic speech rather than by the detector.

Run it before trusting a report. If a report's numbers ever look implausible,
this tells you whether to suspect the analyzer or the recording.

## QA checks at ingest

`earshot ingest` refuses to let a bad run pass quietly. It flags:

- a barge-in scenario whose interruption never landed inside an agent turn
  (re-run that call — the agent probably was not talking when you spoke);
- a barge-in scenario recorded in mono, which cannot be scored at all;
- a call with no agent speech (wrong channel mapping, or it never connected);
- a call under 10s, which is almost always an abandoned attempt.
