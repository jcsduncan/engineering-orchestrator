import json
import sys
from pathlib import Path

import fire
import pytest

from engineer import cli
from engineer.core import Plan


def test_fire_accepts_checks_and_paths(monkeypatch, capsys, tmp_path):
    captured = {}

    def fake_run_task(**kwargs):
        captured.update(kwargs)
        return {"status": "ready_for_review"}

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    fire.Fire(
        cli.run,
        command=[
            "Fix test",
            "--repo",
            str(tmp_path),
            "--model",
            "local-test",
            "--base-url",
            "http://localhost:11434/v1",
            "--worker",
            "qwen -p",
            "--image",
            "test-image",
            '--check=["ruff check .", "pytest -q"]',
            "--state-dir",
            str(tmp_path / "state"),
            "--worker-timeout",
            "60",
            "--run-id",
            "task-123",
            "--mode",
            "investigation",
        ],
    )

    assert captured["repo"] == Path(tmp_path)
    assert captured["worker"] == ["qwen", "-p"]
    assert captured["checks"] == ["ruff check .", "pytest -q"]
    assert captured["worker_timeout"] == 60
    assert captured["run_id"] == "task-123"
    assert captured["execution_image"] == "test-image"
    assert captured["mode"] == "investigation"
    assert json.loads(capsys.readouterr().out) == {"status": "ready_for_review"}


def test_fire_accepts_numeric_run_id(monkeypatch, capsys):
    captured = {}

    def fake_run_task(**kwargs):
        captured.update(kwargs)
        return {"status": "needs_review"}

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    fire.Fire(
        cli.run,
        command=[
            "Fix test",
            "--model",
            "local-test",
            "--worker",
            "qwen -p",
            "--image",
            "test-image",
            "--run-id",
            "12345",
        ],
    )

    assert captured["run_id"] == "12345"
    assert json.loads(capsys.readouterr().out) == {"status": "needs_review"}


@pytest.mark.parametrize("check", [5, ["pytest -q", 5], {"command": "pytest -q"}, ("pytest -q",)])
def test_cli_rejects_invalid_checks(check, monkeypatch):
    monkeypatch.setattr(cli, "run_task", lambda **_kwargs: pytest.fail("worker must not run"))
    with pytest.raises(SystemExit, match="^--check must be a command or a list of commands$"):
        cli.run("Fix test", model="local-test", worker="qwen -p", check=check)


def test_cli_prints_plan_without_worker(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "plan_task",
        lambda *_args: Plan("Inspect code", ["Read files"], ["Run tests"], []),
    )
    monkeypatch.setattr(cli, "run_task", lambda **_kwargs: pytest.fail("worker must not run"))

    cli.run("Inspect code", model="local-test")

    assert json.loads(capsys.readouterr().out) == {
        "objective": "Inspect code",
        "steps": ["Read files"],
        "checks": ["Run tests"],
        "risks": [],
    }


def test_cli_requires_model(monkeypatch):
    monkeypatch.delenv("ENGINEER_MODEL", raising=False)
    with pytest.raises(SystemExit, match="^Set --model or ENGINEER_MODEL$"):
        cli.run("Inspect code")


def test_run_id_requires_worker():
    with pytest.raises(SystemExit, match="^--run-id requires --worker$"):
        cli.run("Inspect code", model="local-test", run_id="task-123")


@pytest.mark.parametrize("mode", ["other", []])
def test_cli_rejects_invalid_mode(mode):
    with pytest.raises(SystemExit, match="^--mode must be implementation or investigation$"):
        cli.run("Inspect code", mode=mode)


def test_cli_requires_container_image(monkeypatch):
    monkeypatch.setattr(cli, "run_task", lambda **_kwargs: pytest.fail("worker must not run"))
    with pytest.raises(SystemExit, match="Set --image to a local worker image"):
        cli.run("Fix test", model="local-test", worker="qwen -p")


def test_cli_status_and_cancel_commands(monkeypatch, capsys, tmp_path):
    captured = {}

    def fake_status(state_dir, run_id):
        captured["status"] = (state_dir, run_id)
        return {"status": "executing"}

    def fake_cancel(state_dir, run_id, reason):
        captured["cancel"] = (state_dir, run_id, reason)
        return {"status": "cancellation_requested"}

    monkeypatch.setattr(cli, "status_run", fake_status)
    monkeypatch.setattr(cli, "cancel_run", fake_cancel)
    monkeypatch.setattr(sys, "argv", ["engineer", "status", "run-1", "--state-dir", str(tmp_path)])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"status": "executing"}
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "engineer",
            "cancel",
            "run-1",
            "--reason",
            "Operator stopped",
            "--state-dir",
            str(tmp_path),
        ],
    )
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"status": "cancellation_requested"}
    assert captured == {
        "status": (tmp_path, "run-1"),
        "cancel": (tmp_path, "run-1", "Operator stopped"),
    }


def test_cli_retry_command_forwards_new_generation(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "run", lambda task, **options: captured.update(task=task, **options))
    monkeypatch.setattr(
        sys,
        "argv",
        ["engineer", "retry", "source-run", "Fix test", "--worker", "qwen -p", "--image", "image"],
    )
    cli.main()
    assert captured == {
        "task": "Fix test",
        "retry_of": "source-run",
        "worker": "qwen -p",
        "image": "image",
    }


def test_cli_cancel_accepts_literal_evaluated_reason(monkeypatch, capsys):
    captured = {}

    def fake_cancel(_state_dir, _run_id, reason):
        captured["reason"] = reason
        return {"status": "cancellation_requested"}

    monkeypatch.setattr(cli, "cancel_run", fake_cancel)
    monkeypatch.setattr(sys, "argv", ["engineer", "cancel", "run-1", "--reason", "123"])
    cli.main()
    assert captured["reason"] == "123"
    assert json.loads(capsys.readouterr().out)["status"] == "cancellation_requested"


def test_cli_review_uses_separate_model(monkeypatch, capsys, tmp_path):
    captured = {}

    def fake_review(state_dir, run_id, reviewer):
        captured.update(state_dir=state_dir, run_id=run_id, reviewer=reviewer)
        return {"status": "completed", "findings": []}

    monkeypatch.setattr(cli, "review_run", fake_review)
    monkeypatch.setattr(
        sys,
        "argv",
        ["engineer", "review", "run-1", "--state-dir", str(tmp_path), "--model", "local-review"],
    )
    cli.main()
    assert captured["state_dir"] == tmp_path
    assert captured["run_id"] == "run-1"
    assert captured["reviewer"].model == "local-review"
    assert json.loads(capsys.readouterr().out) == {"status": "completed", "findings": []}


@pytest.mark.parametrize("alias", ["--check", "--c", "-c", "-check"])
def test_cli_rejects_repeated_check_flags(monkeypatch, alias):
    monkeypatch.setattr(
        sys, "argv", ["engineer", "Fix test", "--check", "first", f"{alias}=second"]
    )
    monkeypatch.setattr(
        cli.fire, "Fire", lambda *_args, **_kwargs: pytest.fail("Fire must not run")
    )
    with pytest.raises(SystemExit, match="^Pass multiple checks as one list"):
        cli.main()
