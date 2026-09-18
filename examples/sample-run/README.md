# Sample run

A complete `earshot` run rendered end to end, so you can see the output shape
without placing a single call.

**The audio here is synthetic** — two fabricated systems with deliberately
opposite turn-taking defects, generated to exercise the pipeline. System A yields
to interruptions in 270ms but also stops for every backchannel; System B never
false-stops but takes 2.1 seconds to yield at all. That mirror-image pair is the
single most useful thing this benchmark surfaces, and a single "barge-in score"
would hide it.

The `judge-result.json` here is a hand-written fixture in the judge's schema, not
a real model output — it exists to exercise the renderer.

| File | What it is |
|---|---|
| `CALL_SHEET.md` | What the tester is handed |
| `analysis.json` | Every measured number, per call and aggregated |
| `judge-result.json` | The judge's scored output |
| `REPORT.md` / `report.html` | The rendered report card |
