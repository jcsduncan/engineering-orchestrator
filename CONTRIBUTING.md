# Contributing

Follow the [coding standards](docs/standards.md). Agents also follow [AGENTS.md](AGENTS.md).

Run `uv sync --locked --dev`, then `uv run --locked pre-commit install`. Before opening a pull request, run `uv run --locked pre-commit run --all-files`, `uv run --locked pytest -q --cov-fail-under=80`, and `uv run --locked mkdocs build --strict`. The full test run enforces at least 80% total branch coverage; focused test runs do not need to meet that project-wide floor.

Add tests for behavior changes and failure paths. Use the PR template to state the behavior changed, checks run, and what remains unverified. Main requires a PR, checks passing against the current base branch, and resolved review threads. The repository owner is requested for review on contributed changes.

Use Conventional Commit subjects for commits and PR titles, such as `fix: reject invalid tasks` or `feat: add issue intake`. CI checks PR titles; Release Please derives versions from these subjects.
