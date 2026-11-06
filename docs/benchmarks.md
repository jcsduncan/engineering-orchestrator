# Reproducible benchmarks

`benchmarks/tasks.json` defines two bounded tasks against versioned fixture source trees. The harness copies each fixture into a fresh Git repository, records its source digest and base commit, then runs the normal local planner and worker. Each case saves `result.json`; the suite saves `summary.json`. Keep these artifacts when comparing controller or model changes.

Run with a reachable local OpenAI-compatible planner and a configured coding worker:

```sh
uv run --locked engineer-bench \
  --manifest benchmarks/tasks.json \
  --output-dir /tmp/engineer-benchmark-1 \
  --model YOUR_LOCAL_MODEL \
  --image YOUR_LOCAL_WORKER_IMAGE \
  --worker 'qwen --approval-mode auto -p'
```

Use a new output directory for each run. The worker, checks, and implementation oracle run in containers; the oracle uses the pinned worker image and a read-only worktree, with no publication credentials. After the run, record observed human intervention counts and measured costs without altering the raw artifacts:

```sh
uv run --locked engineer-bench-measure /tmp/engineer-benchmark-1 \
  --interventions='{"negative_totals":0}' \
  --costs-usd='{"negative_totals":0.12}'
```

This writes `measured_summary.json`. Omitted values remain `null` rather than being treated as zero. Latency measures controller execution and excludes the implementation oracle. Compare scores only for runs with the same manifest, fixture digests, and image IDs.

Implementation correctness requires a reviewable change and a passing oracle. Investigation correctness checks a reviewable evidenced report for the fixture's required terms. This lexical check is a small regression signal; a human must still assess the factual claim. An inconclusive investigation has its own outcome and is never counted as a failed implementation.

Review the saved implementation candidates with a separately selected local model:

```sh
uv run --locked engineer-bench-review /tmp/engineer-benchmark-1 \
  --model YOUR_LOCAL_REVIEW_MODEL
```

The command saves `review.json` for each reviewable implementation case and `review_summary.json` for the suite. It leaves the raw `result.json` and `summary.json` untouched. The impact proxy compares accepting every reviewable candidate with holding candidates that receive a major or critical finding; it counts incorrect candidates flagged and correct candidates flagged against each case's oracle. A finding may be unrelated to the oracle failure, so human adjudication remains necessary. Compare runs with the same manifest, fixtures, and worker image. The suite contains one implementation case, so run it across multiple worker outputs before drawing conclusions about review quality.
