# Contributing

## Adding a scenario

Scenarios live in `earshot/batteries/default.yaml`. A good one provokes **one** specific
failure that a vendor demo would not show you.

```yaml
- id: S19
  name: DTMF fallback
  intent: One sentence on what failure this is designed to provoke.
  dimensions: [turn_taking, task]     # keys from earshot/rubric.py
  setup: { channel: handset, noise: none }
  barge_expectation: none             # yield | hold | none
  turns:
    - { wait: after_agent, say: "..." }
    - { wait: during_agent, offset_ms: 2000, say: "..." }   # an interruption
    - { wait: fixed, offset_ms: 2500, say: "..." }          # after silence
  tester_script: |
    How a human runs this by hand, and what to watch.
  pass_signals: ["..."]
  fail_signals: ["..."]
```

Then add the id to the relevant `Dimension.scenarios` list in `earshot/rubric.py`
so it shows up in the rubric, and to a preset in `earshot/cli.py` if it belongs
in `quick` or `core`.

Rules of thumb:

- **One failure per scenario.** If it fails, you should know why without
  listening again.
- **State the single correct end state** in `tester_script` where there is one.
  "Thursday the 12th for five" is checkable; "handles it well" is not.
- **Prefer things vendors do not demo.** S11 (false barge-in) and S12 (premature
  endpointing) find more real problems than S10, which everyone optimizes for.

## Touching the analyzer

Any change to `earshot/audio.py` must keep `earshot selftest` passing. If a
change moves a measurement, update `earshot/selftest.py`'s `EXPECT` in the same
commit and say in the message why the ground truth moved — a silent tolerance
bump is how a benchmark stops meaning anything.

Please do not add a metric that a model estimates. Measured numbers live in
`audio.py`; everything judged lives behind `earshot/prompts/judge.md`. Keeping that line
sharp is what makes the report worth reading.

## Running the checks

```bash
.venv/bin/earshot selftest    # analyzer calibration
.venv/bin/earshot doctor      # toolchain
python -m compileall earshot  # syntax
```
