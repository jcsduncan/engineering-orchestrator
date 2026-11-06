"""Controller delivery of persisted issue outcomes."""

import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from engineer.core import execute_task
from engineer.delivery import RunDeliveryStore, deliver_finalized, deliver_run
from engineer.intake import IssueClaim, IssueClaimStore, TaskSnapshot
from engineer.providers import Plan, ProviderInfo, WorkerResult
from engineer.publication import PublicationIssue, publish_run
from engineer.scheduler import QueuedRun, RunQueue


class Planner:
    info = ProviderInfo("test-planner", {})

    def plan(self, _task):
        return Plan("Update README", ["Edit"], ["Test"], ["Unknown risk"])


class Worker:
    info = ProviderInfo("test-worker", {})

    def __init__(self, mode):
        self.mode = mode

    def execute(self, _prompt, worktree, _run_id):
        if self.mode == "implementation":
            (worktree / "README.md").write_text("after\n")
            return WorkerResult(0, "Done", "")
        report = {
            "outcome": self.mode,
            "summary": "Observed behavior" if self.mode == "substantiated" else "Cause unknown",
            "findings": [{"claim": "Observed", "evidence": ["README.md:1"]}]
            if self.mode == "substantiated"
            else [],
            "commands": [{"argv": ["cat", "README.md"], "result": "before"}],
            "uncertainty": [] if self.mode == "substantiated" else ["Need logs"],
        }
        return WorkerResult(0, "ENGINEERING_REPORT_JSON=" + json.dumps(report), "")


class Reviewer:
    info = ProviderInfo("test-reviewer", {})

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def review(self, context):
        self.calls += 1
        assert context.diff_sha256
        if self.fail:
            raise RuntimeError("model offline")
        return {"findings": []}


class Remote:
    def __init__(self):
        self.sha = None
        self.pull = None
        self.creates = 0
        self.pushes = 0

    def verify_repository(self, issue, base_sha):
        assert issue.repository_id == 42
        assert len(base_sha) == 40
        return "main"

    def branch_sha(self, _issue, _branch):
        return self.sha

    def find_pull(self, _issue, _branch, _base):
        return self.pull

    def get_pull(self, _issue, number):
        assert number == 7
        return self.pull

    def push(self, _issue, worktree, _git_dir, _branch):
        self.pushes += 1
        self.sha = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def create_pull(self, issue, branch, base, title, body):
        self.creates += 1
        assert title == "feat: update README"
        assert "Automated review (advisory)" in body
        self.pull = {
            "number": 7,
            "html_url": "https://github.com/owner/repo/pull/7",
            "head": {"sha": self.sha, "ref": branch},
            "base": {"ref": base},
            "state": "open",
            "body": body,
        }
        return self.pull


class Queue:
    def __init__(self, snapshot, run_status):
        self.item = QueuedRun(snapshot.run_id, "7:42", snapshot, "completed")
        self.outcome = {"status": "completed", "run_status": run_status}

    def get(self, run_id):
        return self.item if run_id == self.item.run_id else None

    def result(self, run_id):
        return self.outcome if run_id == self.item.run_id else None

    def finalized(self):
        return [self.item]


def make_run(tmp_path, *, mode="implementation"):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    (repo / "README.md").write_text("before\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    state_dir = tmp_path / "state"
    result = execute_task(
        task="Update README",
        repo=repo,
        planner=Planner(),
        worker=Worker(mode),
        checks=[f"{sys.executable} -c 'print(1)'"] if mode == "implementation" else [],
        state_dir=state_dir,
        run_id="issue-1",
        mode="implementation" if mode == "implementation" else "investigation",
    )
    snapshot = TaskSnapshot(
        run_id="issue-1",
        installation_id=7,
        repository_id=42,
        owner="owner",
        repository="repo",
        issue_number=5,
        generation=1,
        title="Update README",
        body="## Outcome\nUpdate README",
        objective="Update README",
        acceptance_criteria="Run tests",
        issue_url="https://github.com/owner/repo/issues/5",
        issue_updated_at="2026-11-06T12:00:00Z",
    )
    claims = IssueClaimStore(state_dir / "runs.sqlite3")
    try:
        claims.claim(snapshot)
        claims.complete(snapshot.run_id)
    finally:
        claims.close()
    return state_dir, result, Queue(snapshot, result["status"])


def test_publish_completed_run_once_across_delivery_restarts(tmp_path):
    state_dir, result, queue = make_run(tmp_path)
    reviewer = Reviewer()
    remote = Remote()
    factory_calls = []

    def remote_factory(snapshot):
        factory_calls.append(snapshot.repository_id)
        return remote

    first = deliver_finalized(state_dir, queue, reviewer, remote_factory)
    second = deliver_finalized(state_dir, queue, reviewer, remote_factory)
    assert first == second
    assert first[0].status == "published"
    assert first[0].pull_url == "https://github.com/owner/repo/pull/7"
    assert result["status"] == "ready_for_review"
    assert reviewer.calls == 1
    assert remote.creates == remote.pushes == 1
    assert factory_calls == [42]


def test_real_queue_finalized_contract(tmp_path):
    state_dir, result, source = make_run(tmp_path)
    queue = RunQueue(state_dir / "runs.sqlite3", max_running=1, max_per_repository=1)
    try:
        queue.enqueue(IssueClaim(source.item.snapshot, "claimed"))
        assert queue.dispatch().run_id == "issue-1"
        queue.finish(
            "issue-1", "completed", {"status": "completed", "run_status": result["status"]}
        )
        remote = Remote()
        outcomes = deliver_finalized(state_dir, queue, Reviewer(), lambda _snapshot: remote)
        assert outcomes[0].status == "published"
        assert remote.creates == 1
    finally:
        queue.close()


def test_retry_after_review_when_publication_credential_unavailable(tmp_path):
    state_dir, _, queue = make_run(tmp_path)
    reviewer = Reviewer()
    remote = Remote()
    attempts = 0

    def remote_factory(_snapshot):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("token unavailable")
        return remote

    failed = deliver_run(state_dir, queue, "issue-1", reviewer, remote_factory)
    assert failed.status == "failed"
    assert "token unavailable" in failed.error
    delivered = deliver_run(state_dir, queue, "issue-1", reviewer, remote_factory)
    assert delivered.status == "published"
    assert reviewer.calls == 1
    assert remote.creates == 1


def test_retry_after_pr_creation_does_not_re_review_or_duplicate_pr(tmp_path, monkeypatch):
    state_dir, _, queue = make_run(tmp_path)
    reviewer = Reviewer()
    remote = Remote()
    original = RunDeliveryStore.finished
    crashed = False

    def crash_once(self, run_id, status, pull_url=None):
        nonlocal crashed
        if status == "published" and not crashed:
            crashed = True
            raise RuntimeError("controller stopped before checkpoint")
        return original(self, run_id, status, pull_url)

    monkeypatch.setattr(RunDeliveryStore, "finished", crash_once)
    first = deliver_run(state_dir, queue, "issue-1", reviewer, lambda _snapshot: remote)
    assert first.status == "failed"
    second = deliver_run(state_dir, queue, "issue-1", reviewer, lambda _snapshot: remote)
    assert second.status == "published"
    assert reviewer.calls == 1
    assert remote.creates == remote.pushes == 1


@pytest.mark.parametrize(
    "mode,expected", [("substantiated", "report_for_review"), ("inconclusive", "inconclusive")]
)
def test_investigation_outcomes_remain_reports(tmp_path, mode, expected):
    state_dir, result, queue = make_run(tmp_path, mode=mode)

    def no_remote(_snapshot):
        raise AssertionError("Investigation must not use publication credentials")

    reviewer = Reviewer()
    delivery = deliver_run(state_dir, queue, "issue-1", reviewer, no_remote)
    assert result["status"] == expected
    assert delivery.status == "report_ready"
    assert delivery.run_status == expected
    assert reviewer.calls == 0
    assert deliver_run(state_dir, queue, "issue-1", reviewer, no_remote) == delivery


def test_stale_worktree_records_failure_without_publication(tmp_path):
    state_dir, result, queue = make_run(tmp_path)
    (Path(result["worktree"]) / "README.md").write_text("changed again\n")

    def no_remote(_snapshot):
        raise AssertionError("Stale diff must not acquire a publication token")

    failed = deliver_run(state_dir, queue, "issue-1", Reviewer(), no_remote)
    assert failed.status == "failed"
    assert "validated diff" in failed.error
    with sqlite3.connect(state_dir / "runs.sqlite3") as db:
        assert db.execute("SELECT attempts FROM run_deliveries").fetchone()[0] == 1


def test_claim_snapshot_mismatch_blocks_review_and_publication(tmp_path):
    state_dir, _, queue = make_run(tmp_path)
    queue.item = replace(queue.item, snapshot=replace(queue.item.snapshot, repository_id=99))
    reviewer = Reviewer()
    failed = deliver_run(
        state_dir,
        queue,
        "issue-1",
        reviewer,
        lambda _snapshot: pytest.fail("Claim mismatch reached publication"),
    )
    assert failed.status == "failed"
    assert "claim" in failed.error
    assert reviewer.calls == 0


def test_advisory_review_error_is_disclosed_and_does_not_approve(tmp_path):
    state_dir, _, queue = make_run(tmp_path)
    reviewer = Reviewer(fail=True)
    remote = Remote()
    delivered = deliver_run(state_dir, queue, "issue-1", reviewer, lambda _snapshot: remote)
    assert delivered.status == "published"
    assert reviewer.calls == 1
    assert "error:" in remote.pull["body"]
    assert "RuntimeError" in remote.pull["body"]


def test_publication_guard_requires_review(tmp_path):
    state_dir, _, queue = make_run(tmp_path)
    snapshot = queue.item.snapshot
    issue = PublicationIssue(
        snapshot.run_id,
        snapshot.installation_id,
        snapshot.repository_id,
        snapshot.owner,
        snapshot.repository,
        snapshot.issue_number,
        snapshot.title,
    )
    with pytest.raises(ValueError, match="requires an advisory review"):
        publish_run(
            state_dir=state_dir,
            issue=issue,
            title="feat: update README",
            remote=Remote(),
            require_review=True,
        )


def test_queue_result_must_match_persisted_run(tmp_path):
    state_dir, _, queue = make_run(tmp_path)
    queue.outcome["run_status"] = "inconclusive"
    failed = deliver_run(
        state_dir, queue, "issue-1", Reviewer(), lambda _snapshot: pytest.fail("published")
    )
    assert failed.status == "failed"
    assert "persisted run evidence" in failed.error
