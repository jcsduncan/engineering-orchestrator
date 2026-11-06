from __future__ import annotations

import contextlib
import os
import signal
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class RunCancelled(Exception):
    """A requested cancellation stopped an execution command."""

    def __init__(self, reason: str, stdout: str = "", stderr: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.stdout = stdout
        self.stderr = stderr
        self.command: str | None = None


@dataclass(frozen=True)
class CancellationProbe:
    database: Path
    external_cancel_reason: Callable[[str], str | None] | None = None

    def __call__(self, run_id: str) -> str | None:
        if not self.database.exists():
            return None
        with contextlib.closing(sqlite3.connect(self.database, timeout=1)) as db:
            row = db.execute(
                "SELECT cancel_reason, status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None or row[0] or self.external_cancel_reason is None:
            return row[0] if row is not None else None
        if row[1] not in {"accepted", "planning", "workspace_pending", "executing", "validating"}:
            return None
        reason = self.external_cancel_reason(run_id)
        if reason is None:
            return None
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
            raise ValueError("External cancellation reason must be 1-500 characters")
        with contextlib.closing(sqlite3.connect(self.database, timeout=1)) as db, db:
            db.execute(
                "UPDATE runs SET cancel_reason = ? WHERE id = ? AND status IN "
                "('accepted', 'planning', 'workspace_pending', 'executing', 'validating') "
                "AND cancel_reason IS NULL",
                (reason.strip(), run_id),
            )
            updated = db.execute(
                "SELECT cancel_reason FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return updated[0] if updated is not None else None


def _drain(stream, tail: bytearray, limit: int) -> None:
    while chunk := stream.read(8192):
        tail.extend(chunk)
        if len(tail) > limit:
            del tail[:-limit]


def run_cancellable_command(
    argv: list[str],
    cwd: Path,
    run_id: str,
    timeout: float,
    output_limit: int,
    cancel_reason: Callable[[str], str | None],
    env: dict[str, str] | None = None,
) -> tuple[int | None, str, str]:
    """Run a host command with bounded cancellation of its process group."""
    if reason := cancel_reason(run_id):
        raise RunCancelled(reason)
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    out_tail, err_tail = bytearray(), bytearray()
    readers = [
        threading.Thread(target=_drain, args=(process.stdout, out_tail, output_limit), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, err_tail, output_limit), daemon=True),
    ]
    for reader in readers:
        reader.start()
    reason = None
    timed_out = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            if reason := cancel_reason(run_id):
                exit_code = None
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                exit_code = None
                break
            try:
                exit_code = process.wait(timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if reason or timed_out or process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        if process.poll() is None:
            process.wait(timeout=2)
        for reader in readers:
            reader.join(timeout=2)
    stdout = out_tail.decode(errors="replace")
    stderr = err_tail.decode(errors="replace")
    if reason:
        raise RunCancelled(reason, stdout, stderr)
    if timed_out:
        stderr = (stderr + f"\nCommand timed out after {timeout} seconds")[-output_limit:]
    return exit_code, stdout, stderr
