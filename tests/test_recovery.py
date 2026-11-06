import shutil
import sqlite3
import subprocess
import sys

import pytest

from engineer.core import _acquire_run_lock, execute_task
from engineer.providers import Plan, ProviderInfo, WorkerResult
from engineer.state import RunStore


class Planner:
    info = ProviderInfo("fixed-planner", {"model": "test"})

    def __init__(self):
        self.calls = 0

    def plan(self, _task):
        self.calls += 1
        return Plan("Update readme", ["Edit"], [], [])


class Worker:
    info = ProviderInfo("fixed-worker", {"command": "test"})

    def __init__(self):
        self.calls = 0

    def execute(self, _prompt, worktree, _run_id):
        self.calls += 1
        (worktree / "README.md").write_text("after\n")
        return WorkerResult(0, "edited", "")


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("before\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    return repo


def run(repo, state_dir, planner, worker, run_id="fixed-run", checks=None):
    return execute_task(
        task="Update readme",
        repo=repo,
        planner=planner,
        worker=worker,
        checks=checks if checks is not None else [f"{sys.executable} -c 'print(1)'"],
        state_dir=state_dir,
        run_id=run_id,
    )


@pytest.mark.parametrize(
    "boundary", ["accepted", "workspace_create", "workspace_transition", "missing_worktree"]
)
def test_safe_resume_after_interruption_before_worker(tmp_path, monkeypatch, boundary):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    planner, worker = Planner(), Worker()
    if boundary == "workspace_create":
        with monkeypatch.context() as patch:
            patch.setattr(
                "engineer.core._ensure_worktree",
                lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()),
            )
            with pytest.raises(KeyboardInterrupt):
                run(repo, state_dir, planner, worker)
    else:
        original = RunStore.transition

        def interrupt(self, run_id, expected, status, **kwargs):
            if (boundary == "accepted" and expected == "accepted") or (
                boundary in {"workspace_transition", "missing_worktree"} and status == "executing"
            ):
                raise KeyboardInterrupt
            return original(self, run_id, expected, status, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(RunStore, "transition", interrupt)
            with pytest.raises(KeyboardInterrupt):
                run(repo, state_dir, planner, worker)

    if boundary == "missing_worktree":
        shutil.rmtree(state_dir / "worktrees/fixed-run")

    result = run(repo, state_dir, planner, worker)
    assert result["status"] == "ready_for_review"
    assert result["id"] == "fixed-run"
    assert planner.calls == 1
    assert worker.calls == 1
    with sqlite3.connect(state_dir / "runs.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    worktrees = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert worktrees.count(f"worktree {state_dir / 'worktrees/fixed-run'}") == 1


def test_interrupted_worker_is_not_repeated(tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    planner = Planner()

    class InterruptedWorker(Worker):
        def execute(self, prompt, worktree, run_id):
            super().execute(prompt, worktree, run_id)
            raise KeyboardInterrupt

    worker = InterruptedWorker()
    with pytest.raises(KeyboardInterrupt):
        run(repo, state_dir, planner, worker)
    result = run(repo, state_dir, planner, worker)
    assert result["status"] == "needs_review"
    assert result["changed_files"] == ["M README.md"]
    assert "commands were not repeated" in result["reason"]
    assert worker.calls == 1
    assert run(repo, state_dir, planner, worker) == result


def test_interrupted_validation_preserves_worker_evidence(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    planner, worker = Planner(), Worker()
    command = [sys.executable, "-c", "print(1)"]
    original = subprocess.run

    def interrupt(argv, *args, **kwargs):
        if argv == command:
            raise KeyboardInterrupt
        return original(argv, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("engineer.core.subprocess.run", interrupt)
        with pytest.raises(KeyboardInterrupt):
            run(repo, state_dir, planner, worker, checks=[f"{sys.executable} -c 'print(1)'"])
    result = run(repo, state_dir, planner, worker, checks=[f"{sys.executable} -c 'print(1)'"])
    assert result["status"] == "needs_review"
    assert result["worker"]["stdout"] == "edited"
    assert result["checks"] == []
    assert worker.calls == 1


def test_interrupted_second_check_preserves_first_check(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    planner, worker = Planner(), Worker()
    checks = [f"{sys.executable} -c 'print(1)'", f"{sys.executable} -c 'print(2)'"]
    original = subprocess.run

    def interrupt(argv, *args, **kwargs):
        if argv == [sys.executable, "-c", "print(2)"]:
            raise KeyboardInterrupt
        return original(argv, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("engineer.core.subprocess.run", interrupt)
        with pytest.raises(KeyboardInterrupt):
            run(repo, state_dir, planner, worker, checks=checks)
    result = run(repo, state_dir, planner, worker, checks=checks)
    assert result["status"] == "needs_review"
    assert len(result["checks"]) == 1
    assert result["checks"][0]["exit_code"] == 0
    assert worker.calls == 1


def test_completed_result_replays_without_repeating_worker(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    planner, worker = Planner(), Worker()
    original = RunStore.transition

    def interrupt_after_commit(self, run_id, expected, status, **kwargs):
        original(self, run_id, expected, status, **kwargs)
        if status == "ready_for_review":
            raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(RunStore, "transition", interrupt_after_commit)
        with pytest.raises(KeyboardInterrupt):
            run(repo, state_dir, planner, worker)
    result = run(repo, state_dir, planner, worker)
    assert result["status"] == "ready_for_review"
    assert worker.calls == 1
    with sqlite3.connect(state_dir / "runs.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 6


def test_run_id_cannot_be_reused_for_different_task(tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    planner, worker = Planner(), Worker()
    run(repo, state_dir, planner, worker)
    with pytest.raises(ValueError, match="different task or configuration"):
        execute_task(
            task="Different task",
            repo=repo,
            planner=planner,
            worker=worker,
            checks=[f"{sys.executable} -c 'print(1)'"],
            state_dir=state_dir,
            run_id="fixed-run",
        )
    with sqlite3.connect(state_dir / "runs.sqlite3") as db:
        assert db.execute("SELECT status FROM runs").fetchone()[0] == "ready_for_review"


def test_concurrent_resume_cannot_claim_same_run(tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    planner, worker = Planner(), Worker()
    lock = _acquire_run_lock(state_dir, "fixed-run")
    try:
        with pytest.raises(RuntimeError, match="active in another controller"):
            run(repo, state_dir, planner, worker)
    finally:
        lock.close()
    assert worker.calls == 0
    assert not (state_dir / "runs.sqlite3").exists()


def test_run_id_rejects_path_traversal(tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    with pytest.raises(ValueError, match="Run ID must be"):
        run(repo, state_dir, Planner(), Worker(), run_id="../escape")
    assert not state_dir.exists()


def test_legacy_running_record_is_reconciled_without_worker(tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    base_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with sqlite3.connect(state_dir / "runs.sqlite3") as db:
        db.execute(
            """CREATE TABLE runs (
                id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, repo TEXT NOT NULL,
                base_sha TEXT NOT NULL, branch TEXT NOT NULL, worktree TEXT NOT NULL,
                status TEXT NOT NULL, task TEXT NOT NULL, plan_json TEXT NOT NULL,
                result_json TEXT
            )"""
        )
        db.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy",
                1,
                str(repo),
                base_sha,
                "agent/legacy",
                str(state_dir / "worktrees/legacy"),
                "running",
                "Update readme",
                '{"objective":"Update readme","steps":[],"checks":[],"risks":[]}',
                None,
            ),
        )
    worker = Worker()
    result = run(repo, state_dir, Planner(), worker, run_id="legacy")
    assert result["status"] == "needs_review"
    assert result["worktree_error"] == "Run worktree is missing"
    assert worker.calls == 0
