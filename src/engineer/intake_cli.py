"""Operator command for one GitHub issue reconciliation pass."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import fire

from .intake import GitHubIssueSource, IssueClaimStore, RepositoryConfig, reconcile


def run(
    owner: str,
    repository: str,
    installation_id: int,
    state_dir: str | None = None,
    generation: int = 1,
    issue_number: int | None = None,
) -> None:
    """Claim open, unblocked issues labelled agent:run in one configured repository."""
    token = os.getenv("ENGINEER_GITHUB_TOKEN")
    if not token:
        raise SystemExit("Set ENGINEER_GITHUB_TOKEN to a GitHub App installation token")
    repo = RepositoryConfig(int(installation_id), str(owner), str(repository))
    path = (
        Path(state_dir).expanduser()
        if state_dir
        else Path.home() / ".local/share/engineering-orchestrator"
    )
    store = IssueClaimStore(path / "runs.sqlite3")
    try:
        result = reconcile(
            GitHubIssueSource(token),
            store,
            repo,
            generation=int(generation),
            issue_number=int(issue_number) if issue_number is not None else None,
        )
    finally:
        store.close()
    print(json.dumps(asdict(result), indent=2))


def main() -> None:
    fire.Fire(run, name="engineer-issues")


if __name__ == "__main__":
    main()
