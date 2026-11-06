import os
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from engineer.cancellation import CancellationProbe, RunCancelled, run_cancellable_command
from engineer.container import ContainerCheckRunner, ContainerRuntime, ContainerWorker
from engineer.core import cancel_run, execute_task, status_run
from engineer.providers import Plan, ProviderInfo, WorkerResult
from engineer.state import MAX_RETRIES, RunStore


class Planner:
    info = ProviderInfo("fixed-planner", {})

    def plan(self, _task):
        return Plan("Change the file", ["Edit"], ["Check"], [])


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


def execute(repo, state_dir, worker, *, run_id, retry_of=None, checks=None, check_runner=None):
    return execute_task(
        task="Change the file",
        repo=repo,
        planner=Planner(),
        worker=worker,
        checks=[] if checks is None else checks,
        state_dir=state_dir,
        run_id=run_id,
        retry_of=retry_of,
        check_runner=check_runner,
    )


def test_cancel_idle_run_and_show_status(tmp_path):
    state_dir = tmp_path / "state"
    store = RunStore(state_dir / "runs.sqlite3")
    store.create(
        run_id="idle",
        repo=tmp_path,
        base_sha="a" * 40,
        branch="agent/idle",
        worktree=state_dir / "worktrees/idle",
        task="Task",
        snapshot={},
        providers={},
    )
    store.close()
    result = cancel_run(state_dir, "idle", "Operator stopped it")
    assert result["status"] == "cancelled"
    assert result["result"]["reason"] == "Operator stopped it"
    assert result["events"] == ["accepted", "cancelled"]
    assert status_run(state_dir, "idle") == result


def test_cancellation_preserves_worker_output_and_changes(tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"

    class Worker:
        info = ProviderInfo("fixed-worker", {})

        def execute(self, _prompt, worktree, run_id):
            (worktree / "README.md").write_text("partial\n")
            store = RunStore(state_dir / "runs.sqlite3")
            store.request_cancel(run_id, "Operator requested")
            store.close()
            return WorkerResult(0, "partial output\n", "")

    result = execute(repo, state_dir, Worker(), run_id="cancel-worker")
    assert result["status"] == "cancelled"
    assert result["reason"] == "Operator requested"
    assert result["worker"]["stdout"] == "partial output\n"
    assert result["changed_files"] == ["M README.md"]
    assert status_run(state_dir, "cancel-worker")["events"][-1] == "cancelled"


def test_cancellation_wins_race_with_worker_failure(tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"

    class Worker:
        info = ProviderInfo("fixed-worker", {})

        def execute(self, _prompt, _worktree, run_id):
            store = RunStore(state_dir / "runs.sqlite3")
            store.request_cancel(run_id, "Stop despite failure")
            store.close()
            raise RuntimeError("worker failed")

    result = execute(repo, state_dir, Worker(), run_id="cancel-race")
    assert result["status"] == "cancelled"
    assert result["reason"] == "Stop despite failure"
    assert status_run(state_dir, "cancel-race")["events"][-1] == "cancelled"


def test_cancellation_preserves_completed_checks_and_current_output(tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"

    class Worker:
        info = ProviderInfo("fixed-worker", {})

        def execute(self, _prompt, worktree, _run_id):
            (worktree / "README.md").write_text("changed\n")
            return WorkerResult(0, "edited", "")

    class CheckRunner:
        calls = 0

        def execute(self, _argv, _worktree, run_id, _timeout):
            self.calls += 1
            if self.calls == 1:
                return WorkerResult(0, "first passed", "")
            store = RunStore(state_dir / "runs.sqlite3")
            store.request_cancel(run_id, "Stop checks")
            store.close()
            raise RunCancelled("Stop checks", "partial check", "")

    runner = CheckRunner()
    result = execute(
        repo,
        state_dir,
        Worker(),
        run_id="cancel-check",
        checks=["echo first", "echo second"],
        check_runner=runner,
    )
    assert result["status"] == "cancelled"
    assert len(result["checks"]) == 1
    assert result["checks"][0]["status"] == "passed"
    assert result["cancelled_check"] == {
        "command": "echo second",
        "stdout": "partial check",
        "stderr": "",
    }


def test_retry_requires_failed_or_cancelled_source_and_is_bounded(tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"

    class FailingWorker:
        info = ProviderInfo("fixed-worker", {})

        def execute(self, _prompt, _worktree, _run_id):
            raise RuntimeError("worker failed")

    worker = FailingWorker()
    with pytest.raises(RuntimeError, match="worker failed"):
        execute(repo, state_dir, worker, run_id="generation-0")
    assert status_run(state_dir, "generation-0")["status"] == "failed"
    with pytest.raises(ValueError, match="differs from source run"):
        execute_task(
            task="Change the file",
            repo=repo,
            planner=Planner(),
            worker=worker,
            checks=[],
            state_dir=state_dir,
            run_id="wrong-mode",
            retry_of="generation-0",
            mode="investigation",
        )
    source = "generation-0"
    for generation in range(1, MAX_RETRIES + 1):
        run_id = f"generation-{generation}"
        with pytest.raises(RuntimeError, match="worker failed"):
            execute(repo, state_dir, worker, run_id=run_id, retry_of=source)
        status = status_run(state_dir, run_id)
        assert status["generation"] == generation
        assert status["parent_id"] == source
        source = run_id
    with pytest.raises(ValueError, match="Retry limit"):
        execute(repo, state_dir, worker, run_id="generation-4", retry_of=source)
    with pytest.raises(ValueError, match="already has a new generation"):
        execute(repo, state_dir, worker, run_id="duplicate", retry_of="generation-0")


def test_retry_rejects_reviewable_source(tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"

    class Worker:
        info = ProviderInfo("fixed-worker", {})

        def execute(self, _prompt, worktree, _run_id):
            (worktree / "README.md").write_text("changed\n")
            return WorkerResult(0, "edited", "")

    worker = Worker()
    first = execute(repo, state_dir, worker, run_id="reviewable", checks=["git diff --check"])
    assert first["status"] == "ready_for_review"
    with pytest.raises(ValueError, match="Only failed or cancelled"):
        execute(repo, state_dir, worker, run_id="retry", retry_of="reviewable")


def test_cancelled_run_retries_in_fresh_generation(tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"

    class Worker:
        info = ProviderInfo("fixed-worker", {})
        calls = 0

        def execute(self, _prompt, worktree, run_id):
            self.calls += 1
            (worktree / "README.md").write_text(f"generation {self.calls}\n")
            if self.calls == 1:
                store = RunStore(state_dir / "runs.sqlite3")
                store.request_cancel(run_id, "Retry this")
                store.close()
            return WorkerResult(0, "edited", "")

    worker = Worker()
    first = execute(repo, state_dir, worker, run_id="first", checks=["git diff --check"])
    assert first["status"] == "cancelled"
    retried = execute(
        repo,
        state_dir,
        worker,
        run_id="second",
        retry_of="first",
        checks=["git diff --check"],
    )
    assert retried["status"] == "ready_for_review"
    assert status_run(state_dir, "second")["generation"] == 1
    assert (state_dir / "worktrees/first/README.md").read_text() == "generation 1\n"
    assert (state_dir / "worktrees/second/README.md").read_text() == "generation 2\n"


def test_host_command_cancellation_keeps_partial_output(tmp_path):
    requested = threading.Event()
    script = (
        "from pathlib import Path; import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', "
        '"import time; from pathlib import Path; '
        "time.sleep(1); Path('child-survived').touch()\"]); "
        "Path('started').touch(); print('partial', flush=True); time.sleep(30)"
    )

    def run():
        return run_cancellable_command(
            [sys.executable, "-c", script],
            tmp_path,
            "host-cancel",
            30,
            1000,
            lambda _run_id: "Operator stopped host" if requested.is_set() else None,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        deadline = time.monotonic() + 5
        while not (tmp_path / "started").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (tmp_path / "started").exists()
        requested.set()
        with pytest.raises(RunCancelled, match="Operator stopped host") as caught:
            future.result(timeout=4)
    assert "partial" in caught.value.stdout
    time.sleep(1.2)
    assert not (tmp_path / "child-survived").exists()


@pytest.mark.skipif(not os.getenv("ENGINEER_TEST_IMAGE"), reason="No local test image configured")
def test_running_container_cancels_with_partial_output(tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    probe = CancellationProbe(state_dir / "runs.sqlite3")
    runtime = ContainerRuntime(
        os.environ["ENGINEER_TEST_IMAGE"], repo / ".git", cancel_reason=probe
    ).resolve_image()
    script = (
        "import time; from pathlib import Path; "
        "Path('started').write_text('yes'); print('partial output', flush=True); time.sleep(30)"
    )
    worker = ContainerWorker(runtime, ("python", "-c", script, "ignored"), timeout=30)

    def run():
        return execute_task(
            task="Change the file",
            repo=repo,
            planner=Planner(),
            worker=worker,
            checks=["python -c 'print(1)'"],
            state_dir=state_dir,
            run_id="docker-cancel",
            check_runner=ContainerCheckRunner(runtime),
            cancel_reason=probe,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        started = state_dir / "worktrees/docker-cancel/started"
        deadline = time.monotonic() + 8
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert started.exists()
        requested = cancel_run(state_dir, "docker-cancel", "Operator stopped Docker")
        assert requested["status"] == "cancellation_requested"
        result = future.result(timeout=12)
    assert result["status"] == "cancelled"
    assert result["reason"] == "Operator stopped Docker"
    assert "partial output" in result["worker"]["stdout"]
    remaining = subprocess.check_output(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=engineering-orchestrator.run=docker-cancel",
            "--format",
            "{{.ID}}",
        ],
        text=True,
    )
    assert not remaining.strip()


@pytest.mark.skipif(not os.getenv("ENGINEER_TEST_IMAGE"), reason="No local test image configured")
def test_running_validation_container_cancels_with_partial_output(tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    probe = CancellationProbe(state_dir / "runs.sqlite3")
    runtime = ContainerRuntime(
        os.environ["ENGINEER_TEST_IMAGE"], repo / ".git", cancel_reason=probe
    ).resolve_image()
    worker_script = "from pathlib import Path; Path('README.md').write_text('changed\\n')"
    check_script = (
        "import time; from pathlib import Path; "
        "Path('check-started').write_text('yes'); "
        "print('partial check', flush=True); time.sleep(30)"
    )

    def run():
        return execute_task(
            task="Change the file",
            repo=repo,
            planner=Planner(),
            worker=ContainerWorker(runtime, ("python", "-c", worker_script, "ignored")),
            checks=[shlex.join(["python", "-c", check_script])],
            state_dir=state_dir,
            run_id="docker-check-cancel",
            check_runner=ContainerCheckRunner(runtime),
            cancel_reason=probe,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        started = state_dir / "worktrees/docker-check-cancel/check-started"
        deadline = time.monotonic() + 8
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert started.exists()
        requested = cancel_run(state_dir, "docker-check-cancel", "Stop validation")
        assert requested["status"] == "cancellation_requested"
        result = future.result(timeout=12)
    assert result["status"] == "cancelled"
    assert result["cancelled_check"]["command"].startswith("python -c")
    assert "partial check" in result["cancelled_check"]["stdout"]
    assert result["worker"]["exit_code"] == 0
    remaining = subprocess.check_output(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=engineering-orchestrator.run=docker-check-cancel",
            "--format",
            "{{.ID}}",
        ],
        text=True,
    )
    assert not remaining.strip()
