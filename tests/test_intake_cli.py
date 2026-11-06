import json

import fire
import pytest

from engineer import intake_cli
from engineer.intake import Issue


class Source:
    def repository_id(self, _repo):
        return 99

    def list_labelled(self, _repo):
        return [7]

    def get_issue(self, _repo, _number):
        return Issue(
            number=7,
            title="Fix cache",
            body="## Outcome\nFix the cache\n\n## Acceptance\n- Add a test",
            state="open",
            labels=frozenset({"agent:run"}),
            url="https://github.com/owner/repo/issues/7",
            updated_at="2026-11-06T12:00:00Z",
        )

    def has_open_blocker(self, _repo, _number):
        return False


def test_fire_reconciles_and_prints_claims(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ENGINEER_GITHUB_TOKEN", "secret")
    monkeypatch.setattr(intake_cli, "GitHubIssueSource", lambda token: Source())
    args = ["owner", "repo", "17", "--state-dir", str(tmp_path)]
    fire.Fire(intake_cli.run, command=args)
    first = json.loads(capsys.readouterr().out)
    assert first["claimed"][0]["snapshot"]["objective"] == "Fix the cache"
    assert first["claimed"][0]["snapshot"]["acceptance_criteria"] == "- Add a test"
    assert "secret" not in json.dumps(first)

    fire.Fire(intake_cli.run, command=args)
    assert json.loads(capsys.readouterr().out) == {"claimed": [], "cancelled": []}


def test_cli_requires_token(monkeypatch, tmp_path):
    monkeypatch.delenv("ENGINEER_GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="Set ENGINEER_GITHUB_TOKEN"):
        intake_cli.run("owner", "repo", 17, state_dir=str(tmp_path))
    assert not (tmp_path / "runs.sqlite3").exists()
