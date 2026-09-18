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
