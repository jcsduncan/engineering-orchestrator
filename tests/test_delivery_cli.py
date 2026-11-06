"""Operator delivery command credential boundaries."""

import json

import pytest

from engineer import delivery_cli
from engineer.delivery import DeliveryResult


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


@pytest.mark.parametrize("status", ["failed", "blocked"])
def test_command_exits_nonzero_when_delivery_needs_attention(tmp_path, monkeypatch, capsys, status):
    monkeypatch.setattr(
        delivery_cli,
        "deliver_finalized",
        lambda *_args, **_kwargs: [DeliveryResult("issue-1", status, "ready_for_review")],
    )
    with pytest.raises(SystemExit) as error:
        delivery_cli.run(7, 42, state_dir=str(tmp_path), review_model="local")
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out)[0]["status"] == status
