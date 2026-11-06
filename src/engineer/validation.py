from __future__ import annotations

import hashlib
import os
import shlex
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from .cancellation import RunCancelled, run_cancellable_command
from .providers import CheckRunner, WorkerResult


class CheckResult(TypedDict):
    command: str
    argv: list[str]
    started_at: str
    duration_seconds: float
    exit_code: int | None
    status: str
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ValidationEvidence:
    checks: list[CheckResult]
    diff_sha256_before: str | None
    diff_sha256_after: str | None
    status: str
    reason: str | None = None


class UnsupportedDiffEntry(ValueError):
    """An untracked entry cannot be represented by the diff hash."""


def _git_bytes(repo: Path, *args: str, git_dir: Path | None = None) -> bytes:
    command = ["git", "-C", str(repo)]
    if git_dir is not None:
        command = [
            "git",
            f"--git-dir={git_dir}",
            f"--work-tree={repo}",
            "-c",
            "core.fsmonitor=false",
        ]
    return subprocess.run([*command, *args], capture_output=True, check=True).stdout


def _update_chunk(digest, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def diff_sha256(worktree: Path, *, git_dir: Path | None = None) -> str:
    """Hash tracked worktree/index changes and untracked files without mutating Git state."""
    digest = hashlib.sha256()
    for args in (
        ("diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames", "HEAD"),
        ("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames", "HEAD"),
    ):
        _update_chunk(digest, _git_bytes(worktree, *args, git_dir=git_dir))

    paths = _git_bytes(
        worktree, "ls-files", "--others", "--exclude-standard", "-z", git_dir=git_dir
    )
    for raw_path in sorted(path for path in paths.split(b"\0") if path):
        path = worktree / os.fsdecode(raw_path)
        mode = path.lstat().st_mode
        _update_chunk(digest, raw_path)
        _update_chunk(digest, str(stat.S_IMODE(mode)).encode())
        if stat.S_ISLNK(mode):
            _update_chunk(digest, os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(mode):
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    _update_chunk(digest, chunk)
        else:
            raise UnsupportedDiffEntry(f"Unsupported untracked entry: {raw_path!r}")
    return digest.hexdigest()


def validate_worktree(
    worktree: Path,
    checks: list[str],
    timeout: int,
    *,
    on_check: Callable[[CheckResult], None] | None = None,
    runner: CheckRunner | None = None,
    run_id: str = "",
    git_guard: Callable[[], None] | None = None,
    git_dir: Path | None = None,
    cancel_reason: Callable[[str], str | None] | None = None,
) -> ValidationEvidence:
    """Run checks in order and bind their results to the final worktree diff."""
    results = []
    try:
        if cancel_reason is not None and (reason := cancel_reason(run_id)):
            raise RunCancelled(reason)
        if git_guard is not None:
            git_guard()
        before = diff_sha256(worktree, git_dir=git_dir)
    except UnsupportedDiffEntry as exc:
        return ValidationEvidence(results, None, None, "invalidated", str(exc))
    for check in checks:
        if cancel_reason is not None and (reason := cancel_reason(run_id)):
            raise RunCancelled(reason)
        argv = shlex.split(check)
        if not argv:
            raise ValueError("Empty check command")
        started_at = datetime.now(UTC).isoformat()
        started = time.monotonic()
        try:
            if runner is None:
                if cancel_reason is None:
                    completed = subprocess.run(
                        argv,
                        check=False,
                        cwd=worktree,
                        capture_output=True,
                        text=True,
                        errors="replace",
                        timeout=timeout,
                    )
                    exit_code = completed.returncode
                    stdout = completed.stdout[-20000:]
                    stderr = completed.stderr[-20000:]
                else:
                    exit_code, stdout, stderr = run_cancellable_command(
                        argv, worktree, run_id, timeout, 20000, cancel_reason
                    )
            else:
                outcome = runner.execute(argv, worktree, run_id, timeout)
                if not isinstance(outcome, WorkerResult):
                    raise ValueError("Check runner returned an invalid WorkerResult")
                exit_code = outcome.exit_code
                stdout = outcome.stdout[-20000:]
                stderr = outcome.stderr[-20000:]
            status = "passed" if exit_code == 0 else "timed_out" if exit_code is None else "failed"
        except RunCancelled as exc:
            exc.command = check
            raise
        except subprocess.TimeoutExpired:
            exit_code = None
            stdout = ""
            stderr = f"Check timed out after {timeout} seconds"
            status = "timed_out"
        except OSError as exc:
            exit_code = None
            stdout = ""
            stderr = str(exc)[-20000:]
            status = "error"
        item: CheckResult = {
            "command": check,
            "argv": argv,
            "started_at": started_at,
            "duration_seconds": time.monotonic() - started,
            "exit_code": exit_code,
            "status": status,
            "stdout": stdout,
            "stderr": stderr,
        }
        results.append(item)
        if on_check is not None:
            on_check(item)
        if status != "passed":
            break
    try:
        if cancel_reason is not None and (reason := cancel_reason(run_id)):
            raise RunCancelled(reason)
        if git_guard is not None:
            git_guard()
        after = diff_sha256(worktree, git_dir=git_dir)
    except UnsupportedDiffEntry as exc:
        return ValidationEvidence(results, before, None, "invalidated", str(exc))
    if before != after:
        status = "invalidated"
    elif not results:
        status = "not_run"
    elif all(result["status"] == "passed" for result in results):
        status = "passed"
    else:
        status = "failed"
    return ValidationEvidence(results, before, after, status)
