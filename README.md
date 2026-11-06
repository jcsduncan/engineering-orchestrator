# Engineering Orchestrator

Engineering tasks run through a local planning model, a coding worker, and explicit validation. Each run has its own Git worktree and a recorded result.

## Use

Planning requires Python 3.11+, Git, and a local OpenAI-compatible model endpoint. Executing a task also requires Docker and a local container image with the selected coding CLI and validation tools.

```bash
uv sync --locked --dev
export ENGINEER_MODEL=your-local-model
export ENGINEER_BASE_URL=http://127.0.0.1:11434/v1
uv run --locked engineer "Investigate the failing test" --repo /path/to/repo
```

Pass a coding CLI and checks to execute a task:

```bash
uv run --locked engineer "Fix the failing test" \
  --repo /path/to/repo \
  --image your-worker-image \
  --worker 'qwen --approval-mode auto -p' \
  --check 'uv run --locked pytest -q'
```

The worker and checks run in disposable containers with the worktree mounted. No model or worker image is bundled.
The `--check` command runs in the target repository, so use that repository's test command.
With the default `--container-network none`, `uv run` needs dependencies already available in the worktree or an offline source; tools installed in the image do not populate the worktree's `.venv`. Use `--container-network bridge` when dependency downloads are required.
For multiple checks, pass a list such as `--check='["uv run --locked ruff check .", "uv run --locked pytest -q"]'`.

Use `uv run --locked engineer status RUN_ID` to inspect a run and `uv run --locked engineer cancel RUN_ID --reason 'Operator requested'` to stop it. Retry a `failed` or `cancelled` run with `uv run --locked engineer retry RUN_ID 'task'` plus the original `--repo`, `--model`, `--image`, `--worker`, `--check`, and `--mode` options. Retry creates a new run at the source base commit and is limited to three generations.

Review a validated change with `uv run --locked engineer review RUN_ID --model LOCAL_REVIEW_MODEL`. The reviewer gets the task, diff, and check evidence without workspace tools. Findings are advisory and tied to the validated diff.

## Documentation

- [System design](docs/HLD.md)
- [Runtime and configuration](docs/runtime.md)
- [Architecture decisions](docs/adr/README.md)
- [Repository tooling](docs/tooling.md)

## Test

```bash
uv run --locked pytest -q --cov-fail-under=80
```

Licensed under [Apache-2.0](LICENSE).
