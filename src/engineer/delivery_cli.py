"""Operator command for one completed-issue delivery pass."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import fire

from .delivery import deliver_finalized
from .publication import GitHubPublicationRemote
from .review import OpenAICompatibleReviewer
from .scheduler import RunQueue


def run(
    state_dir: str | None = None,
    review_model: str | None = None,
    review_base_url: str | None = None,
    review_timeout: int = 120,
    token_file: str | None = None,
) -> None:
    """Review and publish completed issue runs; leave investigations as reports."""
    path = (
        Path(state_dir).expanduser()
        if state_dir
        else Path.home() / ".local/share/engineering-orchestrator"
    )
    model = review_model or os.getenv("ENGINEER_REVIEW_MODEL") or os.getenv("ENGINEER_MODEL")
    if not model:
        raise SystemExit("Set ENGINEER_REVIEW_MODEL or pass --review-model")
    reviewer = OpenAICompatibleReviewer(
        model,
        review_base_url or os.getenv("ENGINEER_BASE_URL", "http://127.0.0.1:11434/v1"),
        int(review_timeout),
    )
    token_path = token_file or os.getenv("ENGINEER_GITHUB_TOKEN_FILE")

    def publication_remote(_snapshot):
        token = (
            Path(token_path).read_text().strip()
            if token_path
            else os.getenv("ENGINEER_GITHUB_TOKEN", "")
        )
        if not token:
            raise ValueError("Set ENGINEER_GITHUB_TOKEN_FILE or ENGINEER_GITHUB_TOKEN")
        return GitHubPublicationRemote(token)

    queue = RunQueue(path / "runs.sqlite3", max_running=1, max_per_repository=1)
    try:
        outcomes = deliver_finalized(path, queue, reviewer, publication_remote)
    finally:
        queue.close()
    print(json.dumps([asdict(outcome) for outcome in outcomes], indent=2))


def main() -> None:
    fire.Fire(run, name="engineer-deliver")


if __name__ == "__main__":
    main()
