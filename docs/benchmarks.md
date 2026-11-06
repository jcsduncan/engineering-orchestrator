# Reproducible benchmarks

`benchmarks/tasks.json` defines two bounded tasks against versioned fixture source trees. The harness copies each fixture into a fresh Git repository, records its source digest and base commit, then runs the normal local planner and worker. Each case saves `result.json`; the suite saves `summary.json`. Keep these artifacts when comparing controller or model changes.

Run with a reachable local OpenAI-compatible planner and a configured coding worker:

```sh
uv run --locked engineer-bench \
  --manifest benchmarks/tasks.json \
  --output-dir /tmp/engineer-benchmark-1 \
  --model YOUR_LOCAL_MODEL \
  --worker 'qwen --approval-mode auto -p'
```

Use a new output directory for each run. Record human intervention counts and measured model costs with `--interventions='{"negative_totals":0}'` and `--costs-usd='{"negative_totals":0.12}'`. Omitted values remain `null` rather than being treated as zero. Latency measures controller execution and excludes the implementation oracle. Compare scores only for runs with the same manifest and fixture digests.

Implementation correctness requires a reviewable change and a passing oracle. Investigation correctness checks a reviewable evidenced report for the fixture's required terms. This lexical check is a small regression signal; a human must still assess the factual claim. An inconclusive investigation has its own outcome and is never counted as a failed implementation.
