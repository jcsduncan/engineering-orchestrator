"""Atomic handoff between durable issue claims and the run queue."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from sqlite3 import IntegrityError, connect
from threading import Barrier

import pytest

from engineer.intake import IssueClaim, IssueClaimStore, TaskSnapshot
from engineer.scheduler import RunQueue


def claim(run_id: str, issue_number: int) -> IssueClaim:
    return IssueClaim(
        TaskSnapshot(
            run_id=run_id,
            installation_id=1,
            repository_id=2,
            owner="owner",
            repository="repo",
            issue_number=issue_number,
            generation=1,
            title="Task",
            body="Body",
            objective="Do work",
            acceptance_criteria="Works",
            issue_url=f"https://github.com/owner/repo/issues/{issue_number}",
            issue_updated_at="2026-01-01T00:00:00Z",
        ),
        "claimed",
    )


def stores(path: Path) -> tuple[IssueClaimStore, RunQueue]:
    return IssueClaimStore(path), RunQueue(path, max_running=2, max_per_repository=2)


def test_accept_claim_is_atomic_and_replay_safe(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite3"
    claims, queue = stores(path)
    try:
        item = claim("issue-one", 1)
        assert claims.claim(item.snapshot) == item
        assert claims.pending_claims(1, 2) == [item]
        assert queue.accept_claim(item).status == "queued"
        assert claims.pending_claims(1, 2) == []
        assert queue.accept_claim(item).status == "queued"
        assert queue.db.execute("SELECT COUNT(*) FROM scheduled_runs").fetchone()[0] == 1
    finally:
        claims.close()
        queue.close()

    claims, queue = stores(path)
    try:
        assert claims.pending_claims(1, 2) == []
        assert queue.accept_claim(item).run_id == "issue-one"
        assert queue.dispatch().run_id == "issue-one"
    finally:
        claims.close()
        queue.close()


def test_simultaneous_handoffs_enqueue_once(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite3"
    claims = IssueClaimStore(path)
    item = claim("issue-one", 1)
    try:
        claims.claim(item.snapshot)
        barrier = Barrier(2)

        def accept() -> str:
            queue = RunQueue(path, max_running=2, max_per_repository=2)
            try:
                barrier.wait()
                return queue.accept_claim(item).run_id
            finally:
                queue.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                future.result(timeout=10) for future in [pool.submit(accept) for _ in range(2)]
            ]
        assert results == [item.snapshot.run_id, item.snapshot.run_id]
        queue = RunQueue(path, max_running=2, max_per_repository=2)
        try:
            assert queue.db.execute("SELECT COUNT(*) FROM scheduled_runs").fetchone()[0] == 1
            assert claims.pending_claims(1, 2) == []
        finally:
            queue.close()
    finally:
        claims.close()


def test_failed_ack_rolls_back_enqueue_and_replays(tmp_path: Path) -> None:
    claims, queue = stores(tmp_path / "runs.sqlite3")
    try:
        item = claim("issue-one", 1)
        claims.claim(item.snapshot)
        claims.db.execute(
            """CREATE TRIGGER reject_dispatch_ack BEFORE UPDATE OF dispatch_ack_at
            ON issue_claims BEGIN SELECT RAISE(ABORT, 'ack failed'); END"""
        )
        with pytest.raises(IntegrityError, match="ack failed"):
            queue.accept_claim(item)
        assert queue.get(item.snapshot.run_id) is None
        assert claims.pending_claims(1, 2) == [item]
        claims.db.execute("DROP TRIGGER reject_dispatch_ack")
        assert queue.accept_claim(item).status == "queued"
        assert claims.pending_claims(1, 2) == []
    finally:
        claims.close()
        queue.close()


def test_accept_recovers_existing_matching_queue_entry(tmp_path: Path) -> None:
    claims, queue = stores(tmp_path / "runs.sqlite3")
    try:
        item = claim("issue-one", 1)
        claims.claim(item.snapshot)
        queue.enqueue(item)
        assert claims.pending_claims(1, 2) == [item]
        assert queue.accept_claim(item).status == "queued"
        assert claims.pending_claims(1, 2) == []
        claims.cancel(item.snapshot.run_id, "Issue closed")
        assert queue.dispatch() is None
        assert queue.get(item.snapshot.run_id).status == "cancelled"
    finally:
        claims.close()
        queue.close()


def test_accept_rejects_forged_or_revoked_snapshot(tmp_path: Path) -> None:
    claims, queue = stores(tmp_path / "runs.sqlite3")
    try:
        item = claim("issue-one", 1)
        claims.claim(item.snapshot)
        changed = IssueClaim(replace(item.snapshot, title="Changed"), "claimed")
        with pytest.raises(ValueError, match="matching active"):
            queue.accept_claim(changed)
        assert queue.get(item.snapshot.run_id) is None
        claims.cancel(item.snapshot.run_id, "Issue closed")
        with pytest.raises(ValueError, match="matching active"):
            queue.accept_claim(item)
        assert queue.get(item.snapshot.run_id) is None
    finally:
        claims.close()
        queue.close()


def test_dispatch_cancels_revoked_claim_and_starts_next(tmp_path: Path) -> None:
    claims, queue = stores(tmp_path / "runs.sqlite3")
    try:
        first, second = claim("issue-one", 1), claim("issue-two", 2)
        for item in (first, second):
            claims.claim(item.snapshot)
            queue.accept_claim(item)
        claims.cancel(first.snapshot.run_id, "agent:run label removed")
        assert queue.dispatch().run_id == second.snapshot.run_id
        assert queue.get(first.snapshot.run_id).status == "cancelled"
        assert queue.result(first.snapshot.run_id) == {"cancel_reason": "agent:run label removed"}
        assert [item.run_id for item in queue.finalized()] == [first.snapshot.run_id]
    finally:
        claims.close()
        queue.close()


def test_missing_guarded_claim_fails_closed(tmp_path: Path) -> None:
    claims, queue = stores(tmp_path / "runs.sqlite3")
    try:
        item = claim("issue-one", 1)
        claims.claim(item.snapshot)
        queue.accept_claim(item)
        with claims.db:
            claims.db.execute("DELETE FROM issue_claims WHERE run_id = ?", (item.snapshot.run_id,))
        assert queue.dispatch() is None
        assert queue.get(item.snapshot.run_id).status == "cancelled"
    finally:
        claims.close()
        queue.close()


def test_concurrent_cancel_and_accept_cannot_start_revoked_claim(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite3"
    claims, queue = stores(path)
    try:
        item = claim("issue-one", 1)
        claims.claim(item.snapshot)
        barrier = Barrier(2)

        def accept() -> bool:
            worker = RunQueue(path, max_running=2, max_per_repository=2)
            try:
                barrier.wait()
                try:
                    worker.accept_claim(item)
                except ValueError:
                    return False
                return True
            finally:
                worker.close()

        def cancel() -> None:
            worker = IssueClaimStore(path)
            try:
                barrier.wait()
                worker.cancel(item.snapshot.run_id, "Issue closed")
            finally:
                worker.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            accepted = pool.submit(accept)
            cancelled = pool.submit(cancel)
            cancelled.result(timeout=10)
            accepted.result(timeout=10)
        assert queue.dispatch() is None
        queued = queue.get(item.snapshot.run_id)
        if queued is not None:
            assert queued.status == "cancelled"
        assert claims.get(item.snapshot.run_id).status == "cancelled"
    finally:
        claims.close()
        queue.close()


def test_finalized_excludes_interrupted_reservation_until_stopped(tmp_path: Path) -> None:
    queue = RunQueue(tmp_path / "runs.sqlite3", max_running=1, max_per_repository=1)
    try:
        item = claim("generic", 1)
        queue.enqueue(item)
        assert queue.dispatch().run_id == item.snapshot.run_id
        queue.recover_interrupted()
        assert queue.finalized() == []
        queue.acknowledge_stopped(item.snapshot.run_id)
        assert [entry.run_id for entry in queue.finalized()] == [item.snapshot.run_id]
    finally:
        queue.close()


def test_existing_queue_database_adds_claim_guard_without_changing_generic_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runs.sqlite3"
    with closing(connect(path)) as db, db:
        db.execute(
            """CREATE TABLE scheduled_runs (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE,
                repository_key TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                cancel_reason TEXT,
                resource_held INTEGER NOT NULL DEFAULT 0,
                result_json TEXT,
                created_at INTEGER NOT NULL,
                started_at INTEGER,
                finished_at INTEGER
            )"""
        )
    queue = RunQueue(path, max_running=1, max_per_repository=1)
    try:
        columns = {row[1] for row in queue.db.execute("PRAGMA table_info(scheduled_runs)")}
        assert "claim_guarded" in columns
        queue.enqueue(claim("generic", 1))
        assert queue.dispatch().run_id == "generic"
    finally:
        queue.close()
