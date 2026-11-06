import json
import sqlite3
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

import engineer.publication as publication_module
from engineer.core import execute_task
from engineer.intake import IssueClaimStore, TaskSnapshot
from engineer.providers import Plan, ProviderInfo, WorkerResult
from engineer.publication import GitHubPublicationRemote, PublicationIssue, publish_run
from engineer.review import review_run
from engineer.validation import diff_sha256


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


class Planner:
    info = ProviderInfo("test", {})

    def plan(self, _task):
        return Plan("Update README", ["Edit"], ["Check"], ["Unknown risk"])


class Worker:
    info = ProviderInfo("test", {})

    def __init__(self, change=True, stage=False):
        self.change = change
        self.stage = stage

    def execute(self, _prompt, worktree, _run_id):
        if self.change:
            (worktree / "README.md").write_text("after\n")
            if self.stage:
                subprocess.run(["git", "-C", str(worktree), "add", "README.md"], check=True)
        return WorkerResult(0, "Done", "")


class FakeRemote:
    def __init__(self):
        self.remote_sha = None
        self.pull = None
        self.pushes = 0
        self.creates = 0
        self.fail_create_once = False
        self.fail_push_once = False

    def verify_repository(self, issue, base_sha):
        assert issue.repository_id == 42
        assert len(base_sha) == 40
        return "main"

    def branch_sha(self, _issue, _branch):
        return self.remote_sha

    def find_pull(self, _issue, _branch, _base):
        return self.pull

    def get_pull(self, _issue, number):
        assert number == 12
        return self.pull

    def create_pull(self, issue, branch, base, title, body):
        if self.fail_create_once:
            self.fail_create_once = False
            raise TimeoutError("PR request timed out")
        self.creates += 1
        assert base == "main"
        assert title == "feat: update README"
        assert f"Closes #{issue.issue_number}" in body
        assert "Validated diff SHA-256" in body
        assert "Unknown risk" in body
        assert "Unverified work" in body
        assert "Automated review (advisory)" in body
        assert "BEGIN_COMMIT_OVERRIDE\nfeat: update README\nEND_COMMIT_OVERRIDE" in body
        self.pull = {
            "number": 12,
            "html_url": "https://github.com/owner/repo/pull/12",
            "head": {"sha": self.remote_sha, "ref": branch},
            "base": {"ref": base},
            "state": "open",
            "body": body,
        }
        return self.pull

    def push(self, _issue, worktree, _git_dir, _branch):
        self.pushes += 1
        if self.fail_push_once:
            self.fail_push_once = False
            raise TimeoutError("push timed out")
        self.remote_sha = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()


def make_run(tmp_path, *, change=True, checks=True, stage=False):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    result = execute_task(
        task="Update README",
        repo=repo,
        planner=Planner(),
        worker=Worker(change, stage),
        checks=[f"{sys.executable} -c 'print(1)'"] if checks else [],
        state_dir=state_dir,
        run_id="publication-1",
    )
    issue = PublicationIssue("publication-1", 7, 42, "owner", "repo", 8, "Update README")
    claim_issue(state_dir, issue)
    return result, issue, state_dir


def claim_issue(state_dir, issue):
    snapshot = TaskSnapshot(
        run_id=issue.run_id,
        installation_id=issue.installation_id,
        repository_id=issue.repository_id,
        owner=issue.owner,
        repository=issue.repository,
        issue_number=issue.issue_number,
        generation=1,
        title=issue.title,
        body="## Outcome\nUpdate README",
        objective="Update README",
        acceptance_criteria="Run checks",
        issue_url=f"https://github.com/{issue.owner}/{issue.repository}/issues/{issue.issue_number}",
        issue_updated_at="2026-11-06T12:00:00Z",
    )
    claims = IssueClaimStore(state_dir / "runs.sqlite3")
    try:
        claims.claim(snapshot)
    finally:
        claims.close()


def test_publish_validated_run_and_replay(tmp_path):
    result, issue, state_dir = make_run(tmp_path)
    remote = FakeRemote()
    first = publish_run(
        state_dir=state_dir, issue=issue, title="feat: update README", remote=remote
    )
    second = publish_run(
        state_dir=state_dir, issue=issue, title="feat: update README", remote=remote
    )
    assert first == second
    assert first.branch == result["branch"]
    assert remote.pushes == 1
    assert remote.creates == 1
    with sqlite3.connect(state_dir / "runs.sqlite3") as db:
        row = db.execute(
            "SELECT repository_id, issue_number, commit_sha, pull_number FROM run_publications"
        ).fetchone()
    assert row == (42, 8, first.commit_sha, 12)
    status = subprocess.run(
        ["git", "-C", result["worktree"], "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status == ""


def test_publication_includes_diff_bound_advisory_findings(tmp_path):
    result, issue, state_dir = make_run(tmp_path)

    class Reviewer:
        info = ProviderInfo("fixture-reviewer", {})

        def review(self, _context):
            return {
                "findings": [
                    {
                        "severity": "major",
                        "file": "README.md",
                        "side": "new",
                        "line": 1,
                        "issue": "Incorrect result",
                        "evidence": "The new line says after",
                    }
                ]
            }

    reviewed = review_run(state_dir, issue.run_id, Reviewer())
    assert reviewed["diff_sha256"] == result["validation"]["diff_sha256_after"]
    remote = FakeRemote()
    publish_run(state_dir=state_dir, issue=issue, title="feat: update README", remote=remote)
    assert 'major "README.md" (new line 1): "Incorrect result"' in remote.pull["body"]
    assert "The new line says after" in remote.pull["body"]


@pytest.mark.parametrize(("change", "checks"), [(False, True), (True, False)])
def test_rejects_unreviewable_run(tmp_path, change, checks):
    _, issue, state_dir = make_run(tmp_path, change=change, checks=checks)
    remote = FakeRemote()
    with pytest.raises(ValueError, match="Only validated implementation runs"):
        publish_run(state_dir=state_dir, issue=issue, title="feat: update README", remote=remote)
    assert remote.pushes == 0


def test_rejects_modified_worktree_after_validation(tmp_path):
    result, issue, state_dir = make_run(tmp_path)
    (state_dir / "worktrees" / issue.run_id / "README.md").write_text("later\n")
    with pytest.raises(ValueError, match="no longer matches validated diff"):
        publish_run(
            state_dir=state_dir, issue=issue, title="feat: update README", remote=FakeRemote()
        )
    assert result["status"] == "ready_for_review"


def test_retry_after_pr_timeout_reuses_branch_and_commit(tmp_path):
    _, issue, state_dir = make_run(tmp_path)
    remote = FakeRemote()
    remote.fail_create_once = True
    with pytest.raises(TimeoutError, match="PR request timed out"):
        publish_run(state_dir=state_dir, issue=issue, title="feat: update README", remote=remote)
    publication = publish_run(
        state_dir=state_dir, issue=issue, title="feat: update README", remote=remote
    )
    assert publication.pull_number == 12
    assert remote.pushes == 1
    assert remote.creates == 1


def test_retry_after_push_timeout_reuses_local_commit(tmp_path):
    _, issue, state_dir = make_run(tmp_path)
    remote = FakeRemote()
    remote.fail_push_once = True
    with pytest.raises(TimeoutError, match="push timed out"):
        publish_run(state_dir=state_dir, issue=issue, title="feat: update README", remote=remote)
    publication = publish_run(
        state_dir=state_dir, issue=issue, title="feat: update README", remote=remote
    )
    assert publication.pull_number == 12
    assert remote.pushes == 2
    assert remote.creates == 1


def test_recovery_accepts_valid_commit_before_sha_recorded(tmp_path):
    _, issue, state_dir = make_run(tmp_path)
    remote = FakeRemote()
    remote.fail_push_once = True
    with pytest.raises(TimeoutError, match="push timed out"):
        publish_run(state_dir=state_dir, issue=issue, title="feat: update README", remote=remote)
    with sqlite3.connect(state_dir / "runs.sqlite3") as db:
        db.execute("UPDATE run_publications SET commit_sha = NULL")
    assert (
        publish_run(
            state_dir=state_dir, issue=issue, title="feat: update README", remote=remote
        ).pull_number
        == 12
    )


def test_recovery_rejects_different_tree_with_matching_parent_and_footer(tmp_path):
    result, issue, state_dir = make_run(tmp_path)
    remote = FakeRemote()
    remote.fail_push_once = True
    with pytest.raises(TimeoutError, match="push timed out"):
        publish_run(state_dir=state_dir, issue=issue, title="feat: update README", remote=remote)
    with sqlite3.connect(state_dir / "runs.sqlite3") as db:
        db.execute("UPDATE run_publications SET commit_sha = NULL")
    (Path(result["worktree"]) / "README.md").write_text("unvalidated\n")
    subprocess.run(["git", "-C", result["worktree"], "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            result["worktree"],
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--amend",
            "--no-verify",
            "-m",
            "feat: update README\n\nRun-ID: publication-1",
        ],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValueError, match="does not match the validated tree"):
        publish_run(state_dir=state_dir, issue=issue, title="feat: update README", remote=remote)


@pytest.mark.parametrize("stage", [False, True])
def test_retry_after_failed_commit_preserves_validated_index(tmp_path, monkeypatch, stage):
    result, issue, state_dir = make_run(tmp_path, stage=stage)
    worktree = result["worktree"]
    validated_diff = diff_sha256(worktree)
    original_command = publication_module._git_command

    def fail_commit(worktree, git_dir, *args):
        if "commit" in args:
            raise OSError("commit failed")
        return original_command(worktree, git_dir, *args)

    monkeypatch.setattr(publication_module, "_git_command", fail_commit)
    remote = FakeRemote()
    with pytest.raises(OSError, match="commit failed"):
        publish_run(state_dir=state_dir, issue=issue, title="feat: update README", remote=remote)
    assert diff_sha256(worktree) == validated_diff
    cached_diff = subprocess.run(
        ["git", "-C", worktree, "diff", "--cached", "--quiet"], check=False
    )
    assert (cached_diff.returncode == 1) is stage
    monkeypatch.setattr(publication_module, "_git_command", original_command)
    assert (
        publish_run(
            state_dir=state_dir, issue=issue, title="feat: update README", remote=remote
        ).pull_number
        == 12
    )


def test_retry_rejects_embedded_run_marker_without_footer(tmp_path):
    result, issue, state_dir = make_run(tmp_path)
    remote = FakeRemote()
    remote.fail_push_once = True
    with pytest.raises(TimeoutError, match="push timed out"):
        publish_run(state_dir=state_dir, issue=issue, title="feat: update README", remote=remote)
    subprocess.run(
        [
            "git",
            "-C",
            result["worktree"],
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--amend",
            "--no-verify",
            "-m",
            "feat: unrelated\n\nRun-ID: publication-1\n\nOther footer",
        ],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValueError, match="unrecognized commit"):
        publish_run(state_dir=state_dir, issue=issue, title="feat: update README", remote=remote)


def test_rejects_remote_branch_with_different_commit(tmp_path):
    _, issue, state_dir = make_run(tmp_path)
    remote = FakeRemote()
    remote.remote_sha = "f" * 40
    with pytest.raises(ValueError, match="Remote run branch points to a different commit"):
        publish_run(state_dir=state_dir, issue=issue, title="feat: update README", remote=remote)
    assert remote.creates == 0


def test_rejects_issue_swap_on_retry(tmp_path):
    _, issue, state_dir = make_run(tmp_path)
    remote = FakeRemote()
    publish_run(state_dir=state_dir, issue=issue, title="feat: update README", remote=remote)
    swapped = PublicationIssue("publication-1", 7, 42, "owner", "repo", 9, "Other")
    with pytest.raises(ValueError, match="Publication issue does not match the stored claim"):
        publish_run(state_dir=state_dir, issue=swapped, title="feat: update README", remote=remote)


def test_rejects_cancelled_claim_before_push(tmp_path):
    _, issue, state_dir = make_run(tmp_path)
    claims = IssueClaimStore(state_dir / "runs.sqlite3")
    try:
        claims.cancel(issue.run_id, "Issue closed")
    finally:
        claims.close()
    remote = FakeRemote()
    with pytest.raises(ValueError, match="no active or completed issue claim"):
        publish_run(state_dir=state_dir, issue=issue, title="feat: update README", remote=remote)
    assert remote.pushes == 0


def test_existing_pr_must_match_run_and_issue(tmp_path):
    _, issue, state_dir = make_run(tmp_path)
    remote = FakeRemote()
    publish_run(state_dir=state_dir, issue=issue, title="feat: update README", remote=remote)
    remote.pull["body"] = "Unrelated PR"
    with pytest.raises(ValueError, match="does not belong to this run and issue"):
        publish_run(state_dir=state_dir, issue=issue, title="feat: update README", remote=remote)
    assert remote.creates == 1


def test_rejects_pr_response_for_another_repository(tmp_path):
    _, issue, state_dir = make_run(tmp_path)

    class WrongRemote(FakeRemote):
        def create_pull(self, *args):
            pull = super().create_pull(*args)
            pull["html_url"] = "https://github.com/other/repo/pull/12"
            return pull

    with pytest.raises(ValueError, match="response does not match this run"):
        publish_run(
            state_dir=state_dir, issue=issue, title="feat: update README", remote=WrongRemote()
        )


def test_replay_of_merged_pr_does_not_recreate_deleted_branch(tmp_path):
    _, issue, state_dir = make_run(tmp_path)
    remote = FakeRemote()
    first = publish_run(
        state_dir=state_dir, issue=issue, title="feat: update README", remote=remote
    )
    remote.pull["state"] = "closed"
    remote.pull["merged_at"] = "2026-11-06T12:00:00Z"
    remote.remote_sha = None
    replay = publish_run(
        state_dir=state_dir, issue=issue, title="feat: update README", remote=remote
    )
    assert replay == first
    assert remote.pushes == 1


def test_rejects_investigation_run(tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"

    class ReportWorker(Worker):
        def execute(self, _prompt, _worktree, _run_id):
            report = {
                "outcome": "inconclusive",
                "summary": "Cause unknown",
                "findings": [],
                "commands": [],
                "uncertainty": ["Need logs"],
            }
            return WorkerResult(0, "ENGINEERING_REPORT_JSON=" + json.dumps(report), "")

    execute_task(
        task="Investigate",
        repo=repo,
        planner=Planner(),
        worker=ReportWorker(),
        checks=[],
        state_dir=state_dir,
        run_id="publication-1",
        mode="investigation",
    )
    issue = PublicationIssue("publication-1", 7, 42, "owner", "repo", 8, "Investigate")
    claim_issue(state_dir, issue)
    with pytest.raises(ValueError, match="Only validated implementation runs"):
        publish_run(
            state_dir=state_dir, issue=issue, title="feat: update README", remote=FakeRemote()
        )


def test_github_remote_rejects_token_with_multiple_repositories(monkeypatch):
    def fake_urlopen(request, timeout):
        assert timeout == 20
        assert request.get_header("Authorization") == "Bearer secret"
        assert request.full_url.endswith("/installation/repositories?per_page=100")
        return BytesIO(json.dumps({"total_count": 2, "repositories": []}).encode())

    monkeypatch.setattr("engineer.publication.urlopen", fake_urlopen)
    issue = PublicationIssue("publication-1", 7, 42, "owner", "repo", 8, "Update README")
    with pytest.raises(ValueError, match="scoped to one repository"):
        GitHubPublicationRemote("secret").verify_repository(issue, "a" * 40)


def test_github_remote_verifies_claimed_repository_and_base(monkeypatch):
    paths = []
    responses = [
        {"total_count": 1, "repositories": [{"id": 42, "full_name": "owner/repo"}]},
        {"id": 42, "default_branch": "main"},
        {"sha": "a" * 40},
    ]

    def fake_urlopen(request, timeout):
        assert timeout == 20
        paths.append(request.full_url)
        return BytesIO(json.dumps(responses.pop(0)).encode())

    monkeypatch.setattr("engineer.publication.urlopen", fake_urlopen)
    issue = PublicationIssue("publication-1", 7, 42, "owner", "repo", 8, "Update README")
    assert GitHubPublicationRemote("secret").verify_repository(issue, "a" * 40) == "main"
    assert paths[-1].endswith("/repos/owner/repo/git/commits/" + "a" * 40)


def test_github_remote_rejects_closed_or_retargeted_pull(monkeypatch):
    remote = GitHubPublicationRemote("secret")
    issue = PublicationIssue("publication-1", 7, 42, "owner", "repo", 8, "Update README")
    pull = {"state": "closed", "merged_at": None, "base": {"ref": "main"}}
    monkeypatch.setattr(remote, "_request", lambda *_args: [pull])
    with pytest.raises(ValueError, match="closed without merging"):
        remote.find_pull(issue, "agent/publication-1", "main")
    pull["state"] = "open"
    pull["base"]["ref"] = "other"
    with pytest.raises(ValueError, match="another base branch"):
        remote.find_pull(issue, "agent/publication-1", "main")


def test_github_remote_missing_branch_is_available(monkeypatch):
    def missing(_request, timeout):
        assert timeout == 20
        raise HTTPError("https://api.github.com/ref", 404, "Not Found", {}, None)

    monkeypatch.setattr("engineer.publication.urlopen", missing)
    issue = PublicationIssue("publication-1", 7, 42, "owner", "repo", 8, "Update README")
    assert GitHubPublicationRemote("secret").branch_sha(issue, "agent/publication-1") is None


def test_git_push_keeps_token_out_of_command(monkeypatch, tmp_path):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        assert "ENGINEER_GITHUB_TOKEN" in kwargs["env"]
        assert "secret" not in " ".join(argv)
        assert "secret" not in (tmp_path / "README.md").read_text()
        return subprocess.CompletedProcess(argv, 0, "", "")

    (tmp_path / "README.md").write_text("public\n")
    monkeypatch.setattr("engineer.publication.subprocess.run", fake_run)
    issue = PublicationIssue("publication-1", 7, 42, "owner", "repo", 8, "Update README")
    GitHubPublicationRemote("secret").push(
        issue, tmp_path, tmp_path / ".git", "agent/publication-1"
    )
    assert captured["argv"][-1] == "HEAD:refs/heads/agent/publication-1"
    assert captured["env"]["HOME"] != str(tmp_path)


def test_git_push_failure_does_not_include_token(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "engineer.publication.subprocess.run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, "", "secret"),
    )
    issue = PublicationIssue("publication-1", 7, 42, "owner", "repo", 8, "Update README")
    with pytest.raises(RuntimeError, match="GitHub branch push failed") as error:
        GitHubPublicationRemote("secret").push(
            issue, tmp_path, tmp_path / ".git", "agent/publication-1"
        )
    assert "secret" not in str(error.value)
