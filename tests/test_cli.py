import json
from pathlib import Path

import fire

from engineer import cli


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
