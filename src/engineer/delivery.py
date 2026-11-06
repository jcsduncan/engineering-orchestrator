"""Deliver persisted issue outcomes after worker execution has finished."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .intake import IssueClaimStore, TaskSnapshot
from .publication import PublicationIssue, PublicationRemote, publish_run, title_from_issue
from .review import ReviewProvider, _review_table, _stored_review, review_run
from .scheduler import RunQueue
from .state import RunStore

DELIVERABLE = {"ready_for_review", "report_for_review", "inconclusive"}
REPORTS = {"report_for_review", "inconclusive"}
TERMINAL = {"published", "report_ready", "suppressed", "blocked"}


class DeliveryInvariantError(ValueError):
    """Persisted identities or evidence disagree and cannot be retried unchanged."""


class DeliveryRemote(PublicationRemote, Protocol):
    """Publication remote that can recheck issue opt-in before writing."""

    def issue_eligible(self, issue: PublicationIssue) -> bool: ...


@dataclass(frozen=True)
class DeliveryResult:
    run_id: str
    status: str
    run_status: str
    pull_url: str | None = None
    error: str | None = None


class RunDeliveryStore:
    """Record delivery attempts and terminal artifacts by immutable run ID."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS run_deliveries (
                    run_id TEXT PRIMARY KEY,
                    run_status TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN
                        ('attempting', 'failed', 'published',
                         'report_ready', 'suppressed', 'blocked')),
                    attempts INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    pull_url TEXT,
                    error TEXT,
                    updated_at INTEGER NOT NULL
                )"""
            )

    def close(self) -> None:
        """Close this delivery store connection."""
        self.db.close()

    def get(self, run_id: str) -> DeliveryResult | None:
        """Read one delivery checkpoint."""
        row = self.db.execute("SELECT * FROM run_deliveries WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return DeliveryResult(
            run_id, row["status"], row["run_status"], row["pull_url"], row["error"]
        )

    def started(self, run_id: str, run_status: str, stage: str) -> DeliveryResult:
        """Persist an attempt before review or external publication calls."""
        if run_status not in DELIVERABLE:
            raise ValueError("Run outcome is not deliverable")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM run_deliveries WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is not None:
                if row["run_status"] != run_status:
                    raise ValueError("Run delivery outcome changed")
                if row["status"] in TERMINAL:
                    return self.get(run_id)
                self.db.execute(
                    """UPDATE run_deliveries SET status = 'attempting', attempts = attempts + 1,
                    stage = ?, error = NULL, updated_at = ? WHERE run_id = ?""",
                    (stage, int(time.time()), run_id),
                )
            else:
                self.db.execute(
                    """INSERT INTO run_deliveries
                    (run_id, run_status, status, attempts, stage, updated_at)
                    VALUES (?, ?, 'attempting', 1, ?, ?)""",
                    (run_id, run_status, stage, int(time.time())),
                )
        return self.get(run_id)

    def stage(self, run_id: str, stage: str) -> None:
        """Record the current side-effect boundary for an active attempt."""
        with self.db:
            self.db.execute(
                """UPDATE run_deliveries SET stage = ?, updated_at = ?
                WHERE run_id = ? AND status = 'attempting'""",
                (stage, int(time.time()), run_id),
            )

    def finished(
        self, run_id: str, status: str, pull_url: str | None = None, reason: str | None = None
    ) -> DeliveryResult:
        """Mark a delivery complete only after its artifact has been persisted."""
        if status not in TERMINAL or (status == "published") != (pull_url is not None):
            raise ValueError("Invalid terminal delivery artifact")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM run_deliveries WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or (
                row["status"] in TERMINAL
                and (row["status"] != status or row["pull_url"] != pull_url)
            ):
                raise ValueError("Delivery terminal artifact conflicts with recorded run")
            self.db.execute(
                """UPDATE run_deliveries SET status = ?, stage = 'done', pull_url = ?,
                error = ?, updated_at = ? WHERE run_id = ?""",
                (status, pull_url, reason, int(time.time()), run_id),
            )
        return self.get(run_id)

    def failed(self, run_id: str, stage: str, error: str) -> DeliveryResult:
        """Retain a bounded error while leaving the run eligible for retry."""
        with self.db:
            self.db.execute(
                """UPDATE run_deliveries SET status = 'failed', stage = ?, error = ?,
                updated_at = ? WHERE run_id = ? AND status = 'attempting'""",
                (stage, error[:500], int(time.time()), run_id),
            )
        return self.get(run_id)


def _run_record(state_dir: Path, queue: RunQueue, run_id: str) -> tuple[dict, TaskSnapshot]:
    """Check the queue, claim, and run records before any delivery side effect."""
    queued = queue.get(run_id)
    outcome = queue.result(run_id)
    if queued is None or queued.status != "completed" or not isinstance(outcome, dict):
        raise DeliveryInvariantError("Run has no completed queue outcome")
    if outcome.get("status") != "completed":
        raise DeliveryInvariantError("Queue result is not a completed outcome")
    claims = IssueClaimStore(state_dir / "runs.sqlite3")
    runs = RunStore(state_dir / "runs.sqlite3")
    try:
        claim = claims.get(run_id)
        record = runs.get(run_id)
    finally:
        claims.close()
        runs.close()
    if claim is not None and claim.status == "claimed":
        raise ValueError("Issue claim finalization is pending")
    if claim is None or claim.status != "completed" or claim.snapshot != queued.snapshot:
        raise DeliveryInvariantError("Completed issue claim does not match queued run")
    if (
        record is None
        or record["status"] != outcome.get("run_status")
        or record["result"] is None
        or record["result"].get("status") != record["status"]
    ):
        raise DeliveryInvariantError("Queue outcome does not match persisted run evidence")
    if record["status"] in REPORTS and record["mode"] != "investigation":
        raise DeliveryInvariantError("Report outcome has the wrong run mode")
    if record["status"] == "ready_for_review" and record["mode"] != "implementation":
        raise DeliveryInvariantError("Implementation outcome has the wrong run mode")
    return record, claim.snapshot


def _publication_state(state_dir: Path, run_id: str) -> dict | None:
    """Find a committed or in-flight publication after a restart."""
    runs = RunStore(state_dir / "runs.sqlite3")
    try:
        row = runs.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'run_publications'"
        ).fetchone()
        if row is None:
            return None
        publication = runs.db.execute(
            "SELECT pull_url FROM run_publications WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(publication) if publication is not None else None
    finally:
        runs.close()


def _persisted_review(state_dir: Path, run_id: str, diff_sha: str) -> dict:
    runs = RunStore(state_dir / "runs.sqlite3")
    try:
        _review_table(runs)
        review = _stored_review(runs, run_id)
    finally:
        runs.close()
    if review is None or review["diff_sha256"] != diff_sha:
        raise DeliveryInvariantError("Publication has no review of the validated diff")
    return review


def deliver_run(
    state_dir: Path,
    queue: RunQueue,
    run_id: str,
    reviewer: ReviewProvider,
    remote_factory: Callable[[TaskSnapshot], DeliveryRemote],
) -> DeliveryResult | None:
    """Deliver one completed issue run, recovering from prior partial attempts."""
    state_dir = state_dir.expanduser().resolve()
    deliveries = RunDeliveryStore(state_dir / "runs.sqlite3")
    try:
        previous = deliveries.get(run_id)
        if previous is not None and previous.status in TERMINAL:
            return previous
        outcome = queue.result(run_id)
        run_status = outcome.get("run_status") if isinstance(outcome, dict) else None
        if run_status not in DELIVERABLE:
            return None
        stage = "preflight"
        started = deliveries.started(run_id, run_status, stage)
        if started.status in TERMINAL:
            return started
        try:
            record, snapshot = _run_record(state_dir, queue, run_id)
            if run_status in REPORTS:
                stage = "report"
                deliveries.stage(run_id, stage)
                return deliveries.finished(run_id, "report_ready")
            stage = "review"
            deliveries.stage(run_id, stage)
            diff_sha = record["result"]["validation"]["diff_sha256_after"]
            publication_state = _publication_state(state_dir, run_id)
            if publication_state is None:
                review_run(state_dir, run_id, reviewer)
            _persisted_review(state_dir, run_id, diff_sha)
            if publication_state is not None and publication_state["pull_url"]:
                return deliveries.finished(run_id, "published", publication_state["pull_url"])
            stage = "publication"
            deliveries.stage(run_id, stage)
            issue = PublicationIssue(
                run_id=snapshot.run_id,
                installation_id=snapshot.installation_id,
                repository_id=snapshot.repository_id,
                owner=snapshot.owner,
                repository=snapshot.repository,
                issue_number=snapshot.issue_number,
                title=snapshot.title,
            )
            remote = remote_factory(snapshot)
            if not remote.issue_eligible(issue):
                return deliveries.finished(
                    run_id,
                    "suppressed",
                    reason=(
                        "Issue is closed or no longer labelled agent:run; inspect any prior branch"
                    ),
                )
            publication = publish_run(
                state_dir=state_dir,
                issue=issue,
                title=title_from_issue(snapshot.title),
                remote=remote,
                require_review=True,
            )
            return deliveries.finished(run_id, "published", publication.pull_url)
        except DeliveryInvariantError as exc:
            return deliveries.finished(run_id, "blocked", reason=str(exc))
        except Exception as exc:
            detail = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            return deliveries.failed(run_id, stage, f"{type(exc).__name__}: {detail}")
    finally:
        deliveries.close()


def deliver_finalized(
    state_dir: Path,
    queue: RunQueue,
    reviewer: ReviewProvider,
    remote_factory: Callable[[TaskSnapshot], DeliveryRemote],
    *,
    repository_key: tuple[int, int] | None = None,
) -> list[DeliveryResult]:
    """Reconcile all persisted completed outcomes in a repeatable pass."""
    results = []
    for queued in queue.finalized():
        if (
            repository_key is not None
            and (
                queued.snapshot.installation_id,
                queued.snapshot.repository_id,
            )
            != repository_key
        ):
            continue
        if queued.status == "completed":
            try:
                result = deliver_run(state_dir, queue, queued.run_id, reviewer, remote_factory)
            except Exception as exc:
                result = DeliveryResult(
                    queued.run_id,
                    "failed",
                    "unknown",
                    error=f"Delivery pass could not record this run: {type(exc).__name__}",
                )
            if result is not None:
                results.append(result)
    return results
