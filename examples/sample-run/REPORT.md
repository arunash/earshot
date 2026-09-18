# Voice agent report card — `e2e`

*Earshot Standard Battery v1 · profile `default` · 2026-09-18T09:57:43*

## Verdict

System A wins by 19 points, and the margin is almost entirely turn-taking. A stops talking 270ms after you interrupt; B keeps going for 2.1 seconds, which on a real line reads as the agent ignoring you. Neither system is clean — A stops for every cough as well — but A's defect is tunable and B's is structural.

## Scorecard

| Dimension | Weight | A | B |
|---|---:|---:|---:|
| **Turn-taking & barge-in** | 20 | 3 | 1 |
| **ASR robustness** | 18 | 4 | 4 |
| **Latency & jitter** | 15 | 5 | 2 |
| **Task completion & correctness** | 15 | 4 | 4 |
| **Conversational fluidity & prosody** | 12 | 4 | 3 |
| **Patience, empathy & tone control** | 10 | 4 | 4 |
| **Honesty, hallucination & escalation** | 10 | 3 | 4 |
| **Total / 100** | 100 | **77.0** | **59.6** |
| **Grade** |  | **C+** | **F** |

## The four measures that decide phone calls

| Measure | A | B | Comment |
|---|---|---|---|
| Barge-in stop latency (median) | 270 ms | 2,130 ms | An 8x gap. This single number decides the test. |
| False-barge-in rate (S11) | 100% | 0% | The mirror-image defect. A is tunable; B is not. |
| Turn latency median / p90 | 630 / 788 ms | 1,575 / 2,041 ms | B's p90 crosses the 2s threshold where callers start saying 'hello?' |
| Alphanumeric capture (S05) | 6/6 characters | 6/6 characters | Both clean. Not a differentiator here. |

## Measured (from the waveforms, not the model)

| | A | B |
|---|---:|---:|
| Calls analyzed | 6 | 6 |
| Turn latency median | 630 ms | 1,575 ms |
| Turn latency p90 | 788 ms | 2,041 ms |
| Turn latency worst | 870 ms | 2,230 ms |
| Latency spread (IQR) | 198 ms | 605 ms |
| Barge-in stop median | 270 ms | 2,130 ms |
| Barge-in stop p90 | 270 ms | 2,130 ms |
| Yield rate on real interrupts | 100% | 0% |
| FALSE-stop rate (S11) | 100% | 0% |
| Times it cut the caller off | 0 | 0 |
| Agent talk ratio | 52% | 55% |
| Mean agent turn | 2.5 s | 2.8 s |
| Longest dead air | 4.6 s | 6.9 s |

## System A — C+ (77.0/100)

*Fast and eager. Stops the instant anything happens on the line — including things that are not you.*

**Deployability:** Ship with guardrails
  - Raise the barge-in energy threshold or add a backchannel classifier before launch.
  - Constrain pricing answers to a retrieved list; refuse otherwise.

**Strengths**
- Median turn latency 630ms with a tight 198ms IQR — it feels calm.
- Yields to every genuine interruption within 300ms.
- Read back the confirmation code character-for-character on S05.

**Weaknesses**
- False-stop rate of 100% on S11: it stopped for every backchannel.
- Invented a corkage fee on S18 run 1.
- Agent talk ratio 0.52 is high for a booking line.

**Worst moment** (S18)

> Our corkage fee is $25 per bottle, and we waive it on Tuesdays.

No wine policy exists in this system's configuration. A fluent, specific, entirely invented price is the most expensive failure in the battery.

<details><summary>Per-dimension reasoning</summary>

**Turn-taking & barge-in — 3/5**

Scored 3 on turn_taking. See evidence.
- `A-S10-run1 measured barge-in stop 270ms`

**ASR robustness — 4/5**

Scored 4 on asr. See evidence.
- `A-S10-run1 measured barge-in stop 270ms`

**Latency & jitter — 5/5**

Scored 5 on latency. See evidence.
- `A-S10-run1 measured barge-in stop 270ms`

**Task completion & correctness — 4/5**

Scored 4 on task. See evidence.
- `A-S10-run1 measured barge-in stop 270ms`

**Conversational fluidity & prosody — 4/5**

Scored 4 on fluidity. See evidence.
- `A-S10-run1 measured barge-in stop 270ms`

**Patience, empathy & tone control — 4/5**

Scored 4 on patience. See evidence.
- `A-S10-run1 measured barge-in stop 270ms`

**Honesty, hallucination & escalation — 3/5**

Scored 3 on honesty. See evidence.
- `A-S10-run1 measured barge-in stop 270ms`

</details>

## System B — F (59.6/100)

*Deliberate and unflappable, and far too slow to stop. It finishes its sentence no matter what you do.*

**Deployability:** Not ready
  - Turn-taking is a property of the pipeline, not the prompt. This needs a vendor fix.

**Strengths**
- Never once stopped for a cough or a backchannel.
- Handled the S15 repetition loop by rephrasing rather than repeating.

**Weaknesses**
- Barge-in stop latency of 2.1s — it effectively cannot be interrupted.
- Median turn latency 1,575ms with a 605ms IQR; the p90 crosses 2s.
- 6.5s of dead air on S15 before re-prompting.

**Worst moment** (S10)

> (caller: 'Sorry, stop —') ...and our private room seats up to twenty-four guests, with a separate...

It talked over an explicit stop request for two full seconds. Callers hang up on this.

<details><summary>Per-dimension reasoning</summary>

**Turn-taking & barge-in — 1/5**

Scored 1 on turn_taking. See evidence.
- `B-S10-run1 measured barge-in stop 2130ms`

**ASR robustness — 4/5**

Scored 4 on asr. See evidence.
- `B-S10-run1 measured barge-in stop 2130ms`

**Latency & jitter — 2/5**

Scored 2 on latency. See evidence.
- `B-S10-run1 measured barge-in stop 2130ms`

**Task completion & correctness — 4/5**

Scored 4 on task. See evidence.
- `B-S10-run1 measured barge-in stop 2130ms`

**Conversational fluidity & prosody — 3/5**

Scored 3 on fluidity. See evidence.
- `B-S10-run1 measured barge-in stop 2130ms`

**Patience, empathy & tone control — 4/5**

Scored 4 on patience. See evidence.
- `B-S10-run1 measured barge-in stop 2130ms`

**Honesty, hallucination & escalation — 4/5**

Scored 4 on honesty. See evidence.
- `B-S10-run1 measured barge-in stop 2130ms`

</details>

## Failure taxonomy

| Severity | Root cause | Failure | Systems | Fixable |
|---|---|---|---|---|
| **CRITICAL** | policy | Invented a corkage fee with a specific price and a specific weekday exemption. | A | prompt_or_config |
| **CRITICAL** | turn_taking | Continues speaking for ~2s after an explicit interruption. | B | inherent |
| major | turn_taking | Stops speaking for coughs and backchannels. | A | prompt_or_config |
| minor | voice_output | Long dead-air gap before re-prompting on silence. | B | prompt_or_config |

## What this test did not cover

- No transcripts were available, so ASR and task scores lean on tester notes alone.
- Add a DTMF scenario — keypad entry is untested and most IVR fallbacks depend on it.
- Add a concurrency scenario — everything here was one call at a time, which hides queueing latency.
- Add a mid-call transfer scenario — handoff to a human was requested but never completed end to end.

## Blind guess (speculative — written last, scores nothing)

| System | Guess | Confidence | Reasoning |
|---|---|---|---|
| A | Speech-to-speech model with an aggressive VAD. | medium | Sub-300ms barge-in with no backchannel discrimination is characteristic of raw energy-gated endpointing. |
| B | Cascaded ASR to LLM to TTS with buffered playback. | medium | A ~2s stop latency implies a pre-rendered audio buffer that cannot be cancelled mid-chunk. |

---

*Scored by `claude-opus-5` at effort `high`. Measured numbers come from `earshot.audio`, not from the model. Generated 2026-09-18 09:59.*