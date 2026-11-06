from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from engineer import controller_cli
from engineer.controller import (
    ControllerSettings,
    RepositoryTarget,
    acknowledge_interrupted,
    load_settings,
    run_pass,
    task_text,
)
from engineer.intake import IssueClaimStore, TaskSnapshot
from engineer.scheduler import RunQueue
from engineer.state import RunStore


def target(tmp_path: Path, repository_id: int = 10) -> RepositoryTarget:
    path = tmp_path / f"repo-{repository_id}"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "base",
        ],
        check=True,
    )
    return RepositoryTarget(
        1, repository_id, "owner", f"repo-{repository_id}", path, ("pytest -q",)
    )


def settings(tmp_path: Path, *targets: RepositoryTarget) -> ControllerSettings:
    return ControllerSettings(
        state_dir=tmp_path / "state",
        targets=targets,
        model="local-planner",
        base_url="http://127.0.0.1:11434/v1",
        worker=("worker", "-p"),
        image="worker-image",
        max_running=2,
        max_per_repository=1,
        poll_interval=0.1,
    )


def snapshot(run_id: str, target: RepositoryTarget, issue_number: int = 1) -> TaskSnapshot:
    return TaskSnapshot(
        run_id=run_id,
        installation_id=target.installation_id,
        repository_id=target.repository_id,
        owner=target.owner,
        repository=target.repository,
        issue_number=issue_number,
        generation=1,
        title="Fix totals",
        body="## Objective\nRepair totals",
        objective="Repair totals",
        acceptance_criteria="Tests pass",
        issue_url=f"https://github.com/owner/{target.repository}/issues/{issue_number}",
        issue_updated_at="2026-01-01T00:00:00Z",
    )


def test_config_requires_explicit_repository_scope(tmp_path: Path) -> None:
    repo = target(tmp_path)
    config = settings(tmp_path, repo)
    assert "Repair totals" in task_text(snapshot("one", repo))
    with pytest.raises(ValueError, match="outside configured"):
        config.target_for(replace(snapshot("one", repo), owner="different"))
    with pytest.raises(ValueError, match="distinct repository identity"):
        settings(tmp_path, repo, repo)
    with pytest.raises(ValueError, match="validation command"):
        replace(repo, checks=())
    with pytest.raises(ValueError, match="HTTP endpoint"):
        replace(config, base_url="not-a-url")


def test_config_file_loads_argument_vectors_without_shell(tmp_path: Path) -> None:
    repo = target(tmp_path)
    config_path = tmp_path / "controller.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "state_dir": str(tmp_path / "state"),
                "model": "local-planner",
                "base_url": "http://127.0.0.1:11434/v1",
                "worker": ["qwen", "-p"],
                "image": "worker-image",
                "repositories": [
                    {
                        "installation_id": 1,
                        "repository_id": 10,
                        "owner": "owner",
                        "repository": "repo-10",
                        "path": str(repo.path),
                        "checks": ["pytest -q"],
                    }
                ],
            }
        )
    )
    loaded = load_settings(config_path)
    assert loaded.worker == ("qwen", "-p")
    assert loaded.targets == (repo,)


def test_dirty_checkout_is_rejected_before_claim_handoff(tmp_path: Path) -> None:
    repo = target(tmp_path)
    config = settings(tmp_path, repo)
    database = config.state_dir / "runs.sqlite3"
    claims = IssueClaimStore(database)
    try:
        claims.claim(snapshot("one", repo))
        (repo.path / "untracked.txt").write_text("dirty")
        with pytest.raises(ValueError, match="must be clean"):
            run_pass(config, execute_run=lambda **kwargs: pytest.fail("Worker started"))
        assert claims.pending_claims(1, 10)[0].snapshot.run_id == "one"
    finally:
        claims.close()


def test_controller_executes_claim_once_and_finalizes(tmp_path: Path) -> None:
    repo = target(tmp_path)
    config = settings(tmp_path, repo)
    database = config.state_dir / "runs.sqlite3"
    claims = IssueClaimStore(database)
    claims.claim(snapshot("one", repo))
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return {"status": "ready_for_review", "id": kwargs["run_id"]}

    try:
        completed = run_pass(config, execute_run=execute)
        assert [(item.run_id, item.status) for item in completed] == [("one", "completed")]
        assert calls[0]["run_id"] == "one"
        assert calls[0]["repo"] == repo.path
        assert calls[0]["worker"] == ["worker", "-p"]
        assert calls[0]["execution_image"] == "worker-image"
        assert calls[0]["checks"] == ["pytest -q"]
        assert claims.get("one").status == "completed"
        assert run_pass(config, execute_run=execute) == []
        assert len(calls) == 1
    finally:
        claims.close()


def test_cancellation_probe_short_circuit_keeps_cancelled_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = target(tmp_path)
    config = settings(tmp_path, repo)
    claims = IssueClaimStore(config.state_dir / "runs.sqlite3")
    claims.claim(snapshot("one", repo))

    def cancelled_at_dispatch(queue, execute, **kwargs):
        item = queue.dispatch()
        result = execute(item, lambda: True)
        assert result == {"status": "cancelled", "reason": "Issue claim revoked"}
        return [queue.finish(item.run_id, result["status"], result)]

    monkeypatch.setattr("engineer.controller.run_queued", cancelled_at_dispatch)
    try:
        completed = run_pass(config, execute_run=lambda **kwargs: pytest.fail("Worker started"))
        assert completed[0].status == "cancelled"
    finally:
        claims.close()


def test_acknowledge_cli_converts_numeric_fire_argument(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(controller_cli, "load_settings", lambda path: object())

    def acknowledge(settings, run_id):
        assert run_id == "12345"
        return SimpleNamespace(run_id=run_id, status="needs_review")

    monkeypatch.setattr(controller_cli, "acknowledge_interrupted", acknowledge)
    controller_cli.acknowledge("config.json", 12345)
    assert json.loads(capsys.readouterr().out)["run_id"] == "12345"


def test_finalization_replays_after_controller_crash(tmp_path: Path) -> None:
    repo = target(tmp_path)
    config = settings(tmp_path, repo)
    database = config.state_dir / "runs.sqlite3"
    claims = IssueClaimStore(database)
    queue = RunQueue(database, max_running=2, max_per_repository=1)
    try:
        claim = claims.claim(snapshot("one", repo))
        queue.accept_claim(claim)
        assert queue.dispatch().run_id == "one"
        queue.finish("one", "completed", {"status": "completed", "run_status": "inconclusive"})
        assert claims.get("one").status == "claimed"
        assert run_pass(config, execute_run=lambda **kwargs: pytest.fail("Run repeated")) == []
        assert claims.get("one").status == "completed"
    finally:
        queue.close()
        claims.close()


def test_revocation_stops_active_run_and_acknowledges_afterward(tmp_path: Path) -> None:
    repo = target(tmp_path)
    config = settings(tmp_path, repo)
    database = config.state_dir / "runs.sqlite3"
    claims = IssueClaimStore(database)
    claims.claim(snapshot("one", repo))
    started = threading.Event()

    def revoke() -> None:
        assert started.wait(3)
        other = IssueClaimStore(database)
        try:
            other.cancel("one", "Issue closed")
        finally:
            other.close()

    canceller = threading.Thread(target=revoke)
    canceller.start()

    def execute(**kwargs):
        started.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if kwargs["external_cancel_reason"]("one"):
                return {"status": "cancelled"}
            time.sleep(0.01)
        pytest.fail("Cancellation was not delivered")

    try:
        completed = run_pass(config, execute_run=execute)
        canceller.join(timeout=3)
        assert [(item.run_id, item.status) for item in completed] == [("one", "cancelled")]
        assert claims.get("one").status == "cancelled"
        assert claims.pending_cancellations(1, 10) == []
    finally:
        claims.close()


def test_revocation_before_handoff_never_starts_a_worker(tmp_path: Path) -> None:
    repo = target(tmp_path)
    config = settings(tmp_path, repo)
    database = config.state_dir / "runs.sqlite3"
    claims = IssueClaimStore(database)
    try:
        claims.claim(snapshot("one", repo))
        claims.cancel("one", "Issue closed")
        assert run_pass(config, execute_run=lambda **kwargs: pytest.fail("Worker started")) == []
        assert claims.pending_cancellations(1, 10) == []
    finally:
        claims.close()


def test_controller_leaves_other_repository_queue_untouched(tmp_path: Path) -> None:
    owned, foreign = target(tmp_path, 10), target(tmp_path, 20)
    config = settings(tmp_path, owned)
    database = config.state_dir / "runs.sqlite3"
    claims = IssueClaimStore(database)
    queue = RunQueue(database, max_running=2, max_per_repository=1)
    try:
        claim = claims.claim(snapshot("foreign", foreign))
        queue.accept_claim(claim)
        with pytest.raises(ValueError, match="outside configured"):
            run_pass(config, execute_run=lambda **kwargs: pytest.fail("Worker started"))
        assert queue.get("foreign").status == "queued"
        assert claims.get("foreign").status == "claimed"
    finally:
        queue.close()
        claims.close()


def test_new_claim_runs_while_another_repository_is_active(tmp_path: Path) -> None:
    first, second = target(tmp_path, 10), target(tmp_path, 20)
    config = settings(tmp_path, first, second)
    database = config.state_dir / "runs.sqlite3"
    claims = IssueClaimStore(database)
    claims.claim(snapshot("first", first))
    first_started = threading.Event()
    second_started = threading.Event()

    def create_second() -> None:
        assert first_started.wait(3)
        other = IssueClaimStore(database)
        try:
            other.claim(snapshot("second", second))
        finally:
            other.close()

    producer = threading.Thread(target=create_second)
    producer.start()

    def execute(**kwargs):
        if kwargs["run_id"] == "first":
            first_started.set()
            assert second_started.wait(3)
        else:
            second_started.set()
        return {"status": "ready_for_review"}

    try:
        completed = run_pass(config, execute_run=execute)
        producer.join(timeout=3)
        assert {item.run_id for item in completed} == {"first", "second"}
    finally:
        claims.close()


def test_interrupted_run_releases_capacity_only_after_container_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = target(tmp_path)
    config = settings(tmp_path, repo)
    database = config.state_dir / "runs.sqlite3"
    claims = IssueClaimStore(database)
    queue = RunQueue(database, max_running=2, max_per_repository=1)
    runs = RunStore(database)
    try:
        claim = claims.claim(snapshot("one", repo))
        queue.accept_claim(claim)
        assert queue.dispatch().run_id == "one"
        runs.create(
            run_id="one",
            repo=repo.path,
            base_sha="a" * 40,
            branch="agent/one",
            worktree=tmp_path / "worktree",
            task="Fix totals",
            snapshot={},
            providers={},
        )
        runs.transition("one", "accepted", "planning")
        assert queue.recover_interrupted() == ["one"]

        def container_present(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, stdout="container-id\n")

        monkeypatch.setattr("engineer.controller.subprocess.run", container_present)
        with pytest.raises(RuntimeError, match="still has a container"):
            acknowledge_interrupted(config, "one")
        assert queue.get("one").resource_held
        assert runs.get("one")["status"] == "planning"

        def no_container(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, stdout="")

        monkeypatch.setattr("engineer.controller.subprocess.run", no_container)
        item = acknowledge_interrupted(config, "one")
        assert item.status == "needs_review"
        assert not item.resource_held
        assert runs.get("one")["status"] == "needs_review"
        assert claims.get("one").status == "completed"
    finally:
        runs.close()
        queue.close()
        claims.close()
