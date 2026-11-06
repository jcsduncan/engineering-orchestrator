import shlex
import subprocess
import sys
from datetime import datetime

import pytest

from engineer.validation import diff_sha256, validate_worktree


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("before\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    return repo


def python_command(script):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def test_validation_records_passed_check_against_unchanged_diff(repo):
    (repo / "README.md").write_text("after\n")
    evidence = validate_worktree(repo, [python_command("print('ok')")], 10)

    assert evidence.status == "passed"
    assert evidence.diff_sha256_before == evidence.diff_sha256_after
    assert evidence.diff_sha256_before == diff_sha256(repo)
    check = evidence.checks[0]
    assert check["argv"] == [sys.executable, "-c", "print('ok')"]
    assert check["exit_code"] == 0
    assert check["status"] == "passed"
    assert check["stdout"] == "ok\n"
    assert check["duration_seconds"] >= 0
    assert datetime.fromisoformat(check["started_at"]).tzinfo is not None


def test_validation_stops_after_failed_check(repo):
    marker = repo / "unexpected.txt"
    evidence = validate_worktree(
        repo,
        [
            python_command("raise SystemExit(7)"),
            python_command("from pathlib import Path; Path('unexpected.txt').write_text('bad')"),
        ],
        10,
    )

    assert evidence.status == "failed"
    assert len(evidence.checks) == 1
    assert evidence.checks[0]["exit_code"] == 7
    assert evidence.checks[0]["status"] == "failed"
    assert not marker.exists()


def test_validation_records_timeout(repo):
    evidence = validate_worktree(repo, [python_command("import time; time.sleep(2)")], 0.01)

    assert evidence.status == "failed"
    assert evidence.checks[0]["status"] == "timed_out"
    assert evidence.checks[0]["exit_code"] is None
    assert evidence.checks[0]["duration_seconds"] >= 0


def test_untracked_file_change_invalidates_validation(repo):
    (repo / "new.txt").write_text("before")
    evidence = validate_worktree(
        repo,
        [python_command("from pathlib import Path; Path('new.txt').write_text('after')")],
        10,
    )

    assert evidence.checks[0]["status"] == "passed"
    assert evidence.status == "invalidated"
    assert evidence.diff_sha256_before != evidence.diff_sha256_after


def test_index_change_invalidates_validation(repo):
    (repo / "README.md").write_text("after\n")
    evidence = validate_worktree(repo, ["git add README.md"], 10)

    assert evidence.checks[0]["status"] == "passed"
    assert evidence.status == "invalidated"
    assert evidence.diff_sha256_before != evidence.diff_sha256_after


def test_no_checks_is_not_validated(repo):
    evidence = validate_worktree(repo, [], 10)

    assert evidence.status == "not_run"
    assert evidence.checks == []
    assert evidence.diff_sha256_before == evidence.diff_sha256_after
