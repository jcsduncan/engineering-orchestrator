# Engineering Orchestrator

A small, model-agnostic engineering task controller. The **orchestrator model runs locally** through an OpenAI-compatible HTTP endpoint. A separate coding CLI works in a new Git worktree. The controller records the plan, worker output, check results, and branch in SQLite.

This is the first vertical slice. It does not yet schedule issues, launch sandboxes, or publish pull requests.

## Quick start

Requires Python 3.11+, Git, a local OpenAI-compatible model server, and a coding CLI.

```bash
uv sync
export ENGINEER_MODEL=your-local-model
export ENGINEER_BASE_URL=http://127.0.0.1:11434/v1
uv run engineer "Investigate the failing test" --repo /path/to/repo
```

The command above asks the local model for a structured plan. To run a coding worker and validation:

```bash
uv run engineer "Fix the failing test" \
  --repo /path/to/repo \
  --worker 'qwen --approval-mode auto -p' \
  --check 'python -m pytest -q'
```

The worker command is split into arguments, then the task prompt is appended as its last argument. It runs in the new worktree. No shell is used for the worker or check commands. The target repository must start clean. Results and worktrees live under `~/.local/share/engineering-orchestrator` by default. The controller leaves branches and worktrees in place for inspection. A run is `ready_for_review` only when the worker exits successfully, every requested check passes, and files changed. Otherwise it is `needs_review`.

The local model server must expose `/v1/chat/completions` and return a JSON plan in the assistant message. Ollama's OpenAI-compatible endpoint is one possible server; the project does not require Ollama specifically. No model is bundled or downloaded.

**Execution boundary:** this first version invokes the coding CLI on the host. Use it only with repositories and worker commands you trust. Container or VM execution and separate publication credentials are the next implementation steps before unattended issue processing.

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

The tests use a fake local model endpoint and a temporary Git repository; they do not require GPU access or an API key.

## Next milestones

1. Replace host execution with a disposable sandbox and enforce resource limits.
2. Add issue intake, idempotent task claiming, cancellation, and bounded retry.
3. Add an independent review pass and draft PR publication through a GitHub App.
4. Evaluate several coding workers and models on the same real task set.
