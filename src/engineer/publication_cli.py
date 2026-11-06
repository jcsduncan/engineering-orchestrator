"""Publish one completed issue claim as a GitHub pull request."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import fire

from .intake import IssueClaimStore
from .publication import GitHubPublicationRemote, PublicationIssue, publish_run, title_from_issue


def run(run_id: str, title: str | None = None, state_dir: str | None = None) -> None:
    """Publish a validated implementation run linked to its claimed GitHub issue."""
    token = os.getenv("ENGINEER_GITHUB_TOKEN")
    if not token:
        raise SystemExit("Set ENGINEER_GITHUB_TOKEN to a one-repository GitHub App token")
    path = (
        Path(state_dir).expanduser()
        if state_dir
        else Path.home() / ".local/share/engineering-orchestrator"
    )
    claims = IssueClaimStore(path / "runs.sqlite3")
    try:
        claim = claims.get(str(run_id))
    finally:
        claims.close()
    if claim is None or claim.status not in {"claimed", "completed"}:
        raise SystemExit("Run has no active or completed GitHub issue claim")
    snapshot = claim.snapshot
    issue = PublicationIssue(
        run_id=snapshot.run_id,
        installation_id=snapshot.installation_id,
        repository_id=snapshot.repository_id,
        owner=snapshot.owner,
        repository=snapshot.repository,
        issue_number=snapshot.issue_number,
        title=snapshot.title,
    )
    pull_title = title or title_from_issue(snapshot.title)
    publication = publish_run(
        state_dir=path,
        issue=issue,
        title=pull_title,
        remote=GitHubPublicationRemote(token),
    )
    print(json.dumps(asdict(publication), indent=2))


def main() -> None:
    fire.Fire(run, name="engineer-publish")


if __name__ == "__main__":
    main()
