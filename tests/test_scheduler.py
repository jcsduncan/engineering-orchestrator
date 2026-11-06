from __future__ import annotations

import fcntl
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


def test_scheduler_rejects_invalid_limits_and_competing_dispatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        RunQueue(tmp_path / "bad.sqlite3", max_running=0, max_per_repository=1)
    queue = RunQueue(tmp_path / "state.sqlite3", max_running=1, max_per_repository=1)
    queue.enqueue(claim("one", 1, 1))
    lock_path = tmp_path / "scheduler.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="another controller"):
            run_queued(queue, lambda item, cancelled: {}, lock_path=lock_path)
    assert queue.get("one").status == "queued"
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


def test_runner_polls_for_new_work_while_another_run_is_active(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    queue = RunQueue(path, max_running=2, max_per_repository=1)
    queue.enqueue(claim("first", 1, 1))
    first_started = threading.Event()
    second_started = threading.Event()

    def produce() -> None:
        assert first_started.wait(2)
        other = RunQueue(path, max_running=2, max_per_repository=1)
        try:
            other.enqueue(claim("second", 2, 1))
        finally:
            other.close()

    producer = threading.Thread(target=produce)
    producer.start()

    def execute(item, should_cancel):
        if item.run_id == "first":
            first_started.set()
            assert second_started.wait(2)
        else:
            second_started.set()
        return {"status": "completed"}

    try:
        completed = run_queued(
            queue, execute, lock_path=tmp_path / "scheduler.lock", poll_interval=0.01
        )
        assert {item.run_id for item in completed} == {"first", "second"}
    finally:
        producer.join(timeout=2)
        queue.close()


def test_runner_rejects_invalid_poll_interval(tmp_path: Path) -> None:
    queue = RunQueue(tmp_path / "state.sqlite3", max_running=1, max_per_repository=1)
    try:
        with pytest.raises(ValueError, match="poll_interval"):
            run_queued(
                queue, lambda item, cancelled: {}, lock_path=tmp_path / "lock", poll_interval=0
            )
    finally:
        queue.close()


def test_unserializable_result_fails_one_run_without_losing_another(tmp_path: Path) -> None:
    queue = RunQueue(tmp_path / "state.sqlite3", max_running=2, max_per_repository=1)
    queue.enqueue(claim("bad", 1, 1))
    queue.enqueue(claim("good", 2, 1))
    barrier = threading.Barrier(2, timeout=2)

    def execute(item, should_cancel):
        barrier.wait()
        if item.run_id == "bad":
            return {"status": "completed", "unserializable": object()}
        return {"status": "completed", "result": "saved"}

    completed = run_queued(queue, execute, lock_path=tmp_path / "scheduler.lock")
    assert {item.run_id: item.status for item in completed} == {
        "bad": "failed",
        "good": "completed",
    }
    assert "not JSON serializable" in queue.result("bad")["error"]
    assert queue.result("good")["result"] == "saved"
    queue.close()


def test_cancellation_is_durable_and_preserves_partial_result(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    queue = RunQueue(path, max_running=1, max_per_repository=1)
    queue.enqueue(claim("queued", 1, 1))
    queue.enqueue(claim("running", 1, 2))
    assert queue.request_cancel("queued", "Issue closed").status == "cancelled"
    assert queue.result("queued") == {"cancel_reason": "Issue closed"}
    assert queue.cancellation_reason("queued") == "Issue closed"
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


def test_cancellation_does_not_stop_an_independent_run(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    queue = RunQueue(path, max_running=2, max_per_repository=1)
    queue.enqueue(claim("cancel-me", 1, 1))
    queue.enqueue(claim("keep-going", 2, 1))
    both_started = threading.Event()
    lock = threading.Lock()
    started = 0

    def cancel_after_start():
        assert both_started.wait(2)
        queue.request_cancel("cancel-me", "Issue closed")

    canceller = threading.Thread(target=cancel_after_start)
    canceller.start()

    def execute(item, should_cancel):
        nonlocal started
        with lock:
            started += 1
            if started == 2:
                both_started.set()
        assert both_started.wait(2)
        if item.run_id == "cancel-me":
            deadline = time.monotonic() + 2
            while not should_cancel() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert should_cancel()
            return {"status": "cancelled", "partial_output": "first step"}
        return {"status": "completed", "result": "unaffected"}

    completed = run_queued(queue, execute, lock_path=tmp_path / "scheduler.lock")
    canceller.join(timeout=2)
    assert {item.run_id: item.status for item in completed} == {
        "cancel-me": "cancelled",
        "keep-going": "completed",
    }
    assert queue.result("cancel-me")["partial_output"] == "first step"
    assert queue.result("keep-going")["result"] == "unaffected"
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
    assert completed == []
    assert recovered.get("uncertain").resource_held is True
    assert [item.run_id for item in recovered.interrupted()] == ["uncertain"]
    assert recovered.get("waiting").status == "queued"
    recovered.acknowledge_stopped("uncertain")
    assert recovered.interrupted() == []
    completed = run_queued(
        recovered,
        lambda item, should_cancel: executed.append(item.run_id) or {"status": "completed"},
        lock_path=tmp_path / "scheduler.lock",
    )
    assert executed == ["waiting"]
    assert [item.run_id for item in completed] == ["waiting"]
    assert recovered.get("uncertain").status == "needs_review"
    assert recovered.result("uncertain")["reason"] == "Controller stopped while run was active"
    with pytest.raises(ValueError, match="no interrupted"):
        recovered.acknowledge_stopped("uncertain")
    recovered.close()
