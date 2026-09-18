# Changelog

## 0.1.0

First release.

- 18-scenario call battery with presets (`quick`, `core`, `full`).
- Waveform analyzer: turn latency distribution, barge-in stop latency,
  expectation-aware yield and **false-stop** rates, caller cut-offs, overlap,
  dead air, talk ratio, turn length.
- `earshot selftest` calibrates the analyzer against synthetic ground truth.
- Seven-dimension rubric with four re-weighting profiles.
- Claude judge with a strict JSON schema; weighted totals and grades computed in
  code so the ranking cannot drift from the scores behind it.
- Blind runs: vendor labels sealed, scenario order shuffled per system,
  `earshot unseal` after the fact.
- Manual mode (you call, with a generated call sheet) and automated mode
  (Twilio, dual-channel recordings).
- Markdown and standalone HTML report cards.

## Unreleased

- `earshot doctor --twilio` verifies the account is active, prints the balance,
  and lists callable numbers — before a run rather than after the first failure.
- Automated calling delivers TwiML **inline** — no tunnel, no server, no public
  URL. `earshot serve` remains a fallback past Twilio's 4000-char limit.
- **Fixed: the agent channel default was inverted.** On Twilio outbound
  dual-channel recordings the answering party is channel 0, not 1, so every
  metric was computed against the harness's own leg. `--agent-channel` now
  defaults to `auto` and picks whichever channel speaks first, warning when the
  margin is under 0.75s.

## Unreleased (noise & network)

- **Robustness ladder** (`--battery robustness`): 21 rungs of one fixed script
  under babble, television, road, traffic and wind at measured SNRs, plus G.726
  and Opus codecs, packet loss concealed and unconcealed, jitter, dropouts, and
  all of it at once.
- `earshot bake` renders the harness's own side of the call with local TTS,
  mixes a **synthesized** noise bed at a measured SNR (no corpus to download,
  reproducible from a seed), and degrades it through a real codec and a lossy
  network.
- **Measurement survives the impairment.** Baking means the exact caller
  timeline is known, so the caller side needs no voice-activity detector. The
  played audio is located in the recording by correlating onset-emphasized
  energy envelopes, which hold where a waveform correlation fails: 0ms alignment
  error and 12ms latency error at -5dB SNR babble, through G.726, and under 20%
  packet loss. `earshot selftest --impaired` guards it.
- New metrics from the timeline: **response rate**, **false triggers** (the
  agent answering the noise), and repeat requests parsed from the transcript.
- Report gains a **scenario matrix** - one row per scenario so you see where a
  system breaks, not just that its average slipped - and **the script**,
  verbatim, because a benchmark that does not publish what it said cannot be
  reproduced.
