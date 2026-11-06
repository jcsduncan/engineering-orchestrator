# Coding standards

- Support Python 3.11 and 3.14. Manage dependencies and commands with `uv`; commit `uv.lock` with dependency changes.
- Use Ruff for formatting and linting. Keep functions focused, name side effects clearly, and type public function parameters and results.
- Keep CLI parsing in `cli.py`. The controller in `core.py` owns state transitions, worktrees, validation, and recorded outcomes; model output is advisory.
- Use pytest for tests. Test observable behavior and failure paths with local fixtures; do not require live models, GitHub credentials, or internet access.
- Keep runtime documentation accurate and concise. Use an ADR for a decision that changes system boundaries or constraints, not for routine implementation work.
- Every PR states the behavior changed, checks run, and known limitations. CI must pass and a human reviews before merge.

Run the local checks in `CONTRIBUTING.md` at the repository root. `AGENTS.md` applies these standards to coding agents.
