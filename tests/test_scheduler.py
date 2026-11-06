from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from engineer.intake import IssueClaim, TaskSnapshot
from engineer.scheduler import RunQueue, run_queued


def claim(run_id: str, repository_id: int, issue_number: int) -> IssueClaim:
    snapshot = TaskSnapshot(
        run_id=run_id,
        installation_id=1,
        repository_id=repository_id,
        owner="owner",
        repository=f"repo-{repository_id}",
        issue_number=issue_number,
        generation=1,
        title="Task",
        body="Body",
        objective="Do work",
        acceptance_criteria="Works",
        issue_url=f"https://github.com/owner/repo-{repository_id}/issues/{issue_number}",
        issue_updated_at="2026-01-01T00:00:00Z",
    )
    return IssueClaim(snapshot, "claimed")


def test_queue_is_idempotent_and_rejects_changed_snapshot(tmp_path: Path) -> None:
    queue = RunQueue(tmp_path / "state.sqlite3", max_running=2, max_per_repository=1)
    first = claim("one", 1, 1)
    assert queue.enqueue(first).status == "queued"
    assert queue.enqueue(first).status == "queued"
    changed = IssueClaim(replace(first.snapshot, title="Changed"), "claimed")
    with pytest.raises(ValueError, match="different issue snapshot"):
        queue.enqueue(changed)
    with pytest.raises(ValueError, match="Only active"):
        queue.enqueue(IssueClaim(first.snapshot, "cancelled"))
    queue.close()


def test_dispatch_respects_global_and_per_repository_limits(tmp_path: Path) -> None:
    queue = RunQueue(tmp_path / "state.sqlite3", max_running=2, max_per_repository=1)
    for item in (claim("a1", 1, 1), claim("a2", 1, 2), claim("b1", 2, 1)):
        queue.enqueue(item)
    assert queue.dispatch().run_id == "a1"
    assert queue.dispatch().run_id == "b1"
    assert queue.dispatch() is None
    queue.finish("a1", "completed", {"status": "ready_for_review"})
    assert queue.dispatch().run_id == "a2"
    queue.close()


def test_runner_executes_independent_repositories_concurrently(tmp_path: Path) -> None:
    queue = RunQueue(tmp_path / "state.sqlite3", max_running=2, max_per_repository=1)
    for item in (claim("a1", 1, 1), claim("a2", 1, 2), claim("b1", 2, 1)):
        queue.enqueue(item)
    barrier = threading.Barrier(2, timeout=2)
    lock = threading.Lock()
    active: dict[str, int] = {}
    starts: list[str] = []

    def execute(item, should_cancel):
        with lock:
            active[item.repository_key] = active.get(item.repository_key, 0) + 1
            assert active[item.repository_key] <= 1
            assert sum(active.values()) <= 2
            starts.append(item.run_id)
        if item.run_id in {"a1", "b1"}:
            barrier.wait()
        time.sleep(0.02)
        with lock:
            active[item.repository_key] -= 1
        return {"status": "completed", "run_id": item.run_id}

    completed = run_queued(queue, execute, lock_path=tmp_path / "scheduler.lock")
    assert {item.run_id for item in completed} == {"a1", "a2", "b1"}
    assert set(starts[:2]) == {"a1", "b1"}
    assert starts[-1] == "a2"
    assert queue.result("a1")["run_id"] == "a1"
    queue.close()


def test_cancellation_is_durable_and_preserves_partial_result(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    queue = RunQueue(path, max_running=1, max_per_repository=1)
    queue.enqueue(claim("queued", 1, 1))
    queue.enqueue(claim("running", 1, 2))
    assert queue.request_cancel("queued", "Issue closed").status == "cancelled"
    assert queue.result("queued") == {"cancel_reason": "Issue closed"}
    started = threading.Event()

    def cancel_after_start():
        assert started.wait(2)
        other = RunQueue(path, max_running=1, max_per_repository=1)
        other.request_cancel("running", "Label removed")
        other.close()

    canceller = threading.Thread(target=cancel_after_start)
    canceller.start()

    def execute(item, should_cancel):
        started.set()
        deadline = time.monotonic() + 2
        while not should_cancel() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert should_cancel()
        return {"status": "cancelled", "partial_output": "examined files"}

    completed = run_queued(queue, execute, lock_path=tmp_path / "scheduler.lock")
    canceller.join(timeout=2)
    assert [item.run_id for item in completed] == ["running"]
    assert completed[0].status == "cancelled"
    assert queue.result("running") == {
        "status": "cancelled",
        "partial_output": "examined files",
        "cancel_reason": "Label removed",
    }
    queue.close()


def test_restart_keeps_queue_and_quarantines_uncertain_run(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    first = RunQueue(path, max_running=1, max_per_repository=1)
    first.enqueue(claim("uncertain", 1, 1))
    first.enqueue(claim("waiting", 1, 2))
    assert first.dispatch().run_id == "uncertain"
    first.close()
    recovered = RunQueue(path, max_running=1, max_per_repository=1)
    executed = []
    completed = run_queued(
        recovered,
        lambda item, should_cancel: executed.append(item.run_id) or {"status": "completed"},
        lock_path=tmp_path / "scheduler.lock",
    )
    assert executed == ["waiting"]
    assert [item.run_id for item in completed] == ["waiting"]
    assert recovered.get("uncertain").status == "needs_review"
    assert recovered.result("uncertain")["reason"] == "Controller stopped while run was active"
    recovered.close()
