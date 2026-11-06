# Repository tooling

| Tool | Repository configuration | Account setup |
| --- | --- | --- |
| GitHub Actions | CI runs lint, tests with an 80% coverage floor, package and container builds, and docs build. | None |
| Main branch ruleset | Changes use a PR with checks passing against current main; review threads must be resolved. Squash is the only merge method. GitHub prefills the subject from the PR title; the merger edits it to plain language. | None |
| CODEOWNERS | Requests review from the repository owner on contributed changes. | None |
| Ruff and pre-commit | `uv run --locked pre-commit run --all-files` | None |
| CodeQL | GitHub default setup | None |
| Dependabot | Weekly uv, Actions, and Docker updates; Actions updates are grouped. PR titles use Conventional Commit prefixes. Only Python development dependency patch updates auto-merge after required checks pass. | None |
| Codecov | CI uploads coverage using GitHub OIDC. | Connect this repository to Codecov. |
| OpenSSF Scorecard | Weekly and main-branch scans publish SARIF. | None |
| Release Please | A releasable change on main opens or updates a release PR. A repository-scoped GitHub App token lets CI run without manual approval and enables squash auto-merge after required checks; the merge publishes the GitHub release and GHCR image. | Install a GitHub App with Contents and Pull requests write access to this repository. Set `RELEASE_APP_CLIENT_ID` as a repository variable and `RELEASE_APP_PRIVATE_KEY` as a repository secret. Until configured, release PRs remain manual. |
| MkDocs | CI builds docs; GitHub Pages publishes them. | Pages source must be GitHub Actions. |
| GHCR | A release publishes the CLI image to `ghcr.io/jcsduncan/engineering-orchestrator`. | Image visibility is managed in GHCR. |
| CodeRabbit | Reviews pull requests. | GitHub App installed for this repository. |

The container includes the planning CLI and Git. It does not bundle a coding worker or a model server. Supply a reachable model endpoint with `ENGINEER_BASE_URL`.

The ruleset requests a PR but requires zero approving reviews while this is a single-maintainer repository. `CODEOWNERS` requests the owner on outside contributions; the owner makes the merge decision.

Local checks:

```bash
uv sync --locked --dev
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q --cov-fail-under=80
uv run --locked mkdocs build --strict
```
