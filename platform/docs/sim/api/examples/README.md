# Example episodes (mock data for fe)

Made by `uv run python -m mockgen` with the real engine and scripted agents. No LLM call. Illustrative, not results.

- `episode-demo.jsonl`: 30 rounds, exploratory. A posts the same offer on `private_A_B` each round and always prices 10.5.
  B is a grim trigger with target 10.5. A cost shock (c + 0.5) is announced at round 8 and acts in rounds 10–14.
- `episode-demo-probe.jsonl`: branch of the run above at round 15 (`branch.parent_run_id`).
  A is forced to the Nash price for one round. B punishes in rounds 16–18, then returns to 10.5.

Both examples are scripted runs, so they have no `talk_replied`, `talk_text` or `llm_call` events.
Those event types appear only in the schema examples of `openapi.yaml`.

Line 1 is the `EpisodeHeader`. Each other line is one `SimEvent` (seq 0 … n). Wall times are fixed, so the files do not change between runs.

## Trail example

- `trail-demo.jsonl`: round 1 of a C1 run on the fake LLM endpoint of the tests (no paid call), one `TrailLine` per
  `llm_call` (DECISIONS-14 §2). The first call failed (HTTP 500) and its request was removed from the cache, so its line
  has `missing: "no_cache_row"`; the retry and the other calls have the request, the tools, the reply and the fake
  `thinking` text. Run ids, times and hashes come from that run. Written by
  `SIM_WRITE_TRAIL_DEMO=1 uv run pytest tests/test_trail_route.py -k demo`; the test checks it against the `TrailLine` schema.
