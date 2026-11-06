import json
import sqlite3
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest

from engineer.core import execute_task, status_run
from engineer.providers import Plan, ProviderInfo, WorkerResult
from engineer.review import OpenAICompatibleReviewer, review_run


class Planner:
    info = ProviderInfo("fixture-planner", {})

    def plan(self, _task):
        return Plan("Change the file", ["Edit"], ["Check"], [])


class Worker:
    info = ProviderInfo("fixture-worker", {})

    def __init__(self, *, untracked=False):
        self.untracked = untracked

    def execute(self, _prompt, worktree, _run_id):
        (worktree / "README.md").write_text("after\n")
        if self.untracked:
            (worktree / "new.txt").write_text("new content\n")
        return WorkerResult(0, "edited", "")


def make_ready(tmp_path, *, untracked=False, checks=True, worker=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("before\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    state_dir = tmp_path / "state"
    result = execute_task(
        task="Change the file",
        repo=repo,
        planner=Planner(),
        worker=worker or Worker(untracked=untracked),
        checks=[f"{sys.executable} -c 'print(1)'"] if checks else [],
        state_dir=state_dir,
        run_id="review-me",
    )
    return state_dir, result


class Reviewer:
    info = ProviderInfo("fixture-reviewer", {"variant": "fixed"})

    def __init__(self, findings=None):
        self.findings = findings
        self.contexts = []

    def review(self, context):
        self.contexts.append(context)
        findings = self.findings
        if findings is None:
            findings = [
                {
                    "severity": "major",
                    "file": "README.md",
                    "side": "new",
                    "line": 1,
                    "issue": "The change omits a required behavior",
                    "evidence": "The added first line only says after",
                }
            ]
        return {"findings": findings}


def test_review_uses_read_only_evidence_and_persists_advisory_findings(tmp_path):
    state_dir, run = make_ready(tmp_path, untracked=True)
    reviewer = Reviewer()
    first = review_run(state_dir, "review-me", reviewer)
    assert first["status"] == "completed"
    assert first["findings"][0]["severity"] == "major"
    assert first["diff_sha256"] == run["validation"]["diff_sha256_after"]
    context = reviewer.contexts[0]
    assert context.task == "Change the file"
    assert "+after" in context.tracked_diff
    assert context.untracked_files == ({"path": "new.txt", "content": "new content\n"},)
    assert context.changed_paths == ("README.md", "new.txt")
    assert context.validation["status"] == "passed"
    assert context.checks[0]["status"] == "passed"
    assert review_run(state_dir, "review-me", reviewer) == first
    assert len(reviewer.contexts) == 1
    assert status_run(state_dir, "review-me")["status"] == "ready_for_review"
    with sqlite3.connect(state_dir / "runs.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM run_reviews").fetchone()[0] == 1


def test_review_rejects_unvalidated_or_changed_diff(tmp_path):
    state_dir, result = make_ready(tmp_path, checks=False)
    assert result["status"] == "needs_review"
    with pytest.raises(ValueError, match="Only validated implementation"):
        review_run(state_dir, "review-me", Reviewer())

    other_root = tmp_path / "other"
    other_root.mkdir()
    other_dir, result = make_ready(other_root)
    (Path(result["worktree"]) / "README.md").write_text("changed again\n")
    with pytest.raises(ValueError, match="no longer matches"):
        review_run(other_dir, "review-me", Reviewer())


def test_cached_review_rejects_later_worktree_change(tmp_path):
    state_dir, result = make_ready(tmp_path)
    reviewer = Reviewer()
    review_run(state_dir, "review-me", reviewer)
    (Path(result["worktree"]) / "README.md").write_text("changed after review\n")
    with pytest.raises(ValueError, match="no longer matches"):
        review_run(state_dir, "review-me", reviewer)
    assert len(reviewer.contexts) == 1


@pytest.mark.parametrize(
    "finding",
    [
        {
            "severity": "major",
            "file": "outside.py",
            "side": "new",
            "line": 1,
            "issue": "x",
            "evidence": "y",
        },
        {
            "severity": "major",
            "file": "README.md",
            "side": "new",
            "line": 0,
            "issue": "x",
            "evidence": "y",
        },
        {
            "severity": "major",
            "file": "README.md",
            "side": "new",
            "line": 99999,
            "issue": "x",
            "evidence": "y",
        },
        {
            "severity": "unknown",
            "file": "README.md",
            "side": "new",
            "line": 1,
            "issue": "x",
            "evidence": "y",
        },
    ],
)
def test_invalid_finding_is_recorded_as_review_error_without_changing_run(tmp_path, finding):
    state_dir, _ = make_ready(tmp_path)
    reviewed = review_run(state_dir, "review-me", Reviewer([finding]))
    assert reviewed["status"] == "error"
    assert reviewed["findings"] == []
    assert "Reviewer finding needs" in reviewed["error"]
    assert status_run(state_dir, "review-me")["status"] == "ready_for_review"


def test_reviewer_cannot_change_validated_files_without_detection(tmp_path):
    state_dir, result = make_ready(tmp_path)

    class MutatingReviewer(Reviewer):
        def review(self, context):
            (Path(result["worktree"]) / "README.md").write_text("tampered\n")
            return super().review(context)

    with pytest.raises(ValueError, match="changed during review"):
        review_run(state_dir, "review-me", MutatingReviewer())
    with sqlite3.connect(state_dir / "runs.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM run_reviews").fetchone()[0] == 0


def test_oversized_evidence_is_recorded_as_unavailable(tmp_path, monkeypatch):
    state_dir, _ = make_ready(tmp_path)
    reviewer = Reviewer()
    monkeypatch.setattr("engineer.review.MAX_REVIEW_BYTES", 10)
    result = review_run(state_dir, "review-me", reviewer)
    assert result["status"] == "unavailable"
    assert result["findings"] == []
    assert "too large" in result["error"]
    assert reviewer.contexts == []
    assert status_run(state_dir, "review-me")["status"] == "ready_for_review"


def test_untracked_symlink_target_is_not_sent_to_reviewer(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("do not send this")

    class SymlinkWorker(Worker):
        def execute(self, prompt, worktree, run_id):
            result = super().execute(prompt, worktree, run_id)
            (worktree / "secret-link").symlink_to(secret)
            return result

    state_dir, result = make_ready(tmp_path, worker=SymlinkWorker())
    assert result["status"] == "ready_for_review"
    reviewer = Reviewer()
    review = review_run(state_dir, "review-me", reviewer)
    assert review["status"] == "unavailable"
    assert reviewer.contexts == []


def test_openai_reviewer_sends_only_evidence_without_tools(monkeypatch, tmp_path):
    state_dir, _ = make_ready(tmp_path)
    captured = {}

    def fake_open(request, timeout):
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        response = {"choices": [{"message": {"content": '{"findings": []}'}}]}
        return BytesIO(json.dumps(response).encode())

    monkeypatch.setattr("engineer.review.urllib.request.urlopen", fake_open)
    reviewer = OpenAICompatibleReviewer("review-model", "http://127.0.0.1:11434/v1")
    reviewed = review_run(state_dir, "review-me", reviewer)
    assert reviewed["status"] == "completed"
    assert reviewed["findings"] == []
    assert captured["body"]["model"] == "review-model"
    assert "tools" not in captured["body"]
    context = json.loads(captured["body"]["messages"][1]["content"])
    assert "worktree" not in context
    assert context["checks"][0]["status"] == "passed"
    assert captured["timeout"] == 120


def test_reviewer_timeout_is_advisory_and_does_not_expose_endpoint(monkeypatch, tmp_path):
    state_dir, _ = make_ready(tmp_path)

    def timed_out(_request, timeout):
        assert timeout == 120
        raise TimeoutError("private endpoint token")

    monkeypatch.setattr("engineer.review.urllib.request.urlopen", timed_out)
    reviewer = OpenAICompatibleReviewer("review-model", "http://127.0.0.1:11434/v1")
    result = review_run(state_dir, "review-me", reviewer)
    assert result["status"] == "error"
    assert result["error"] == "TimeoutError"
    assert status_run(state_dir, "review-me")["status"] == "ready_for_review"
    response = {"choices": [{"message": {"content": '{"findings": []}'}}]}
    monkeypatch.setattr(
        "engineer.review.urllib.request.urlopen",
        lambda _request, timeout: BytesIO(json.dumps(response).encode()),
    )
    retry = review_run(
        state_dir,
        "review-me",
        OpenAICompatibleReviewer("other-review-model", "http://127.0.0.1:11434/v1"),
    )
    assert retry["status"] == "completed"
    assert retry["provider"]["configuration"]["model"] == "other-review-model"
