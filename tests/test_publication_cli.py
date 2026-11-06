import json

import pytest

from engineer import publication_cli
from engineer.intake import IssueClaimStore, TaskSnapshot
from engineer.publication import Publication


def claim_run(tmp_path, *, status="claimed", title="Fix cache leak"):
    snapshot = TaskSnapshot(
        run_id="issue-7",
        installation_id=17,
        repository_id=99,
        owner="owner",
        repository="repo",
        issue_number=7,
        generation=1,
        title=title,
        body="## Outcome\nFix cache leak",
        objective="Fix cache leak",
        acceptance_criteria="Add a regression test",
        issue_url="https://github.com/owner/repo/issues/7",
        issue_updated_at="2026-11-06T12:00:00Z",
    )
    store = IssueClaimStore(tmp_path / "runs.sqlite3")
    try:
        store.claim(snapshot)
        if status == "cancelled":
            store.cancel(snapshot.run_id, "Issue closed")
        elif status == "completed":
            store.complete(snapshot.run_id)
    finally:
        store.close()


@pytest.mark.parametrize("status", ["claimed", "completed"])
def test_publish_cli_uses_claimed_issue_and_token(tmp_path, monkeypatch, capsys, status):
    claim_run(tmp_path, status=status)
    monkeypatch.setenv("ENGINEER_GITHUB_TOKEN", "secret")
    captured = {}

    def fake_publish(**kwargs):
        captured.update(kwargs)
        return Publication("issue-7", "agent/issue-7", "a" * 40, 12, "https://example/pull/12")

    monkeypatch.setattr(publication_cli, "GitHubPublicationRemote", lambda token: token)
    monkeypatch.setattr(publication_cli, "publish_run", fake_publish)

    publication_cli.run("issue-7", state_dir=str(tmp_path))

    assert captured["issue"].issue_number == 7
    assert captured["issue"].repository_id == 99
    assert captured["remote"] == "secret"
    assert captured["title"] == "fix: cache leak"
    assert json.loads(capsys.readouterr().out)["pull_number"] == 12


def test_publish_cli_rejects_cancelled_or_missing_claim(tmp_path, monkeypatch):
    claim_run(tmp_path, status="cancelled")
    monkeypatch.setenv("ENGINEER_GITHUB_TOKEN", "secret")
    with pytest.raises(SystemExit, match="no active or completed"):
        publication_cli.run("issue-7", state_dir=str(tmp_path))
    with pytest.raises(SystemExit, match="no active or completed"):
        publication_cli.run("missing", state_dir=str(tmp_path))


def test_publish_cli_requires_token(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGINEER_GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="one-repository GitHub App token"):
        publication_cli.run("issue-7", state_dir=str(tmp_path))


def test_title_override_reaches_publisher(tmp_path, monkeypatch):
    claim_run(tmp_path, title="Add feature")
    monkeypatch.setenv("ENGINEER_GITHUB_TOKEN", "secret")
    captured = {}
    monkeypatch.setattr(publication_cli, "GitHubPublicationRemote", lambda _token: object())

    def fake_publish(**kwargs):
        captured.update(kwargs)
        return Publication("issue-7", "agent/issue-7", "a" * 40, 12, "https://example/pull/12")

    monkeypatch.setattr(publication_cli, "publish_run", fake_publish)
    publication_cli.run("issue-7", title="feat: add feature", state_dir=str(tmp_path))
    assert captured["title"] == "feat: add feature"
