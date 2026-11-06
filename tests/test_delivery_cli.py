"""Operator delivery command credential boundaries."""

import json

import pytest

from engineer import delivery_cli


def test_token_file_is_read_when_publication_starts(tmp_path, monkeypatch, capsys):
    token_file = tmp_path / "app-token"
    token_file.write_text("first\n")
    seen = []

    class Remote:
        def __init__(self, token):
            seen.append(token)

    def fake_deliver(_path, _queue, _reviewer, factory, *, repository_key):
        assert repository_key == (7, 42)
        factory(object())
        token_file.write_text("rotated\n")
        factory(object())
        return []

    monkeypatch.setattr(delivery_cli, "GitHubPublicationRemote", Remote)
    monkeypatch.setattr(delivery_cli, "deliver_finalized", fake_deliver)
    delivery_cli.run(
        7, 42, state_dir=str(tmp_path), review_model="local", token_file=str(token_file)
    )
    assert seen == ["first", "rotated"]
    assert json.loads(capsys.readouterr().out) == []


def test_report_only_pass_never_reads_publication_token(tmp_path, monkeypatch, capsys):
    def fake_deliver(_path, _queue, _reviewer, _factory, *, repository_key):
        assert repository_key == (7, 42)
        return []

    monkeypatch.delenv("ENGINEER_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(delivery_cli, "deliver_finalized", fake_deliver)
    delivery_cli.run(7, 42, state_dir=str(tmp_path), review_model="local")
    assert json.loads(capsys.readouterr().out) == []


def test_rejects_token_file_inside_worker_worktree(tmp_path):
    token_file = tmp_path / "worktrees" / "issue-1" / "token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("secret")
    with pytest.raises(ValueError, match="outside worker worktrees"):
        delivery_cli.run(
            7, 42, state_dir=str(tmp_path), review_model="local", token_file=str(token_file)
        )
