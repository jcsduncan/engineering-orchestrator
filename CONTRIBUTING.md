# Contributing

Follow the [coding standards](docs/standards.md). Agents also follow [AGENTS.md](AGENTS.md).

Run `uv sync --locked --dev`, then `uv run --locked pre-commit install`. Before opening a pull request, run `uv run --locked pre-commit run --all-files`, `uv run --locked pytest -q --cov-fail-under=80`, and `uv run --locked mkdocs build --strict`. The full test run enforces at least 80% total branch coverage; focused test runs do not need to meet that project-wide floor.

Add tests for behavior changes and failure paths. Use the PR template to state the behavior changed, checks run, and what remains unverified. Main requires a PR, checks passing against the current base branch, and resolved review threads. The repository owner is requested for review on contributed changes.

Use Conventional Commit subjects for PR titles, such as `fix: reject invalid tasks` or `feat: add issue intake`. CI checks PR titles. Main accepts squash merges only. Before merging, edit GitHub's proposed squash commit title to a plain imperative summary, such as `Reject invalid tasks`. GitHub cannot remove the PR title prefix automatically.

For a change that needs a release, add a commit override to the PR description. Use the PR title's subject if its type is `feat` or `fix`; otherwise choose a release-triggering subject that accurately describes the change:

```text
BEGIN_COMMIT_OVERRIDE
feat: add issue intake
END_COMMIT_OVERRIDE
```

Release Please reads this from the merged PR. Put any `BREAKING CHANGE:` footer inside the override. `chore:` changes do not trigger a release.
