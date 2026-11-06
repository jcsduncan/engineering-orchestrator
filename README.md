# Engineering Orchestrator

Engineering tasks run through a local planning model, a coding worker, and explicit validation. Each run has its own Git worktree and a recorded result.

## Use

Requires Python 3.11+, Git, and a local OpenAI-compatible model endpoint. Executing a task also requires the selected coding CLI.

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
  --worker 'qwen --approval-mode auto -p' \
  --check 'uv run --locked pytest -q'
```

The worker runs on the host in a new worktree. Use trusted repositories and worker commands. No model is bundled.
The `--check` command runs in the target repository, so use that repository's test command.
For multiple checks, pass a list such as `--check='["uv run --locked ruff check .", "uv run --locked pytest -q"]'`.

## Documentation

- [System design](docs/HLD.md)
- [Runtime and configuration](docs/runtime.md)
- [Architecture decisions](docs/adr/README.md)
- [Repository tooling](docs/tooling.md)

## Test

```bash
uv run --locked pytest -q
```

Licensed under [Apache-2.0](LICENSE).
