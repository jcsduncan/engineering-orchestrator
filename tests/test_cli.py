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
            '--check=["ruff check .", "pytest -q"]',
            "--state-dir",
            str(tmp_path / "state"),
            "--worker-timeout",
            "60",
        ],
    )

    assert captured["repo"] == Path(tmp_path)
    assert captured["worker"] == ["qwen", "-p"]
    assert captured["checks"] == ["ruff check .", "pytest -q"]
    assert captured["worker_timeout"] == 60
    assert json.loads(capsys.readouterr().out) == {"status": "ready_for_review"}


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
