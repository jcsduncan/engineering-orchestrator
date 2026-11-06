"""Durable, bounded dispatch of independently claimed issue runs."""

from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, CancelledError, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path

from .intake import IssueClaim, TaskSnapshot

FINAL_STATUSES = {"completed", "failed", "cancelled", "needs_review"}


@dataclass(frozen=True)
class QueuedRun:
    run_id: str
    repository_key: str
    snapshot: TaskSnapshot
    status: str
    cancel_requested: bool = False
    resource_held: bool = False


class RunQueue:
    """Persist queue order, occupancy, cancellation, and final outcomes in SQLite."""

    def __init__(self, path: Path, *, max_running: int, max_per_repository: int) -> None:
        if any(type(value) is not int or value <= 0 for value in (max_running, max_per_repository)):
            raise ValueError("Scheduler limits must be positive integers")
        self.path = path
        self.max_running = max_running
        self.max_per_repository = max_per_repository
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS scheduled_runs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    repository_key TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN
                        ('queued', 'running', 'completed', 'failed', 'cancelled', 'needs_review')),
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    cancel_reason TEXT,
                    resource_held INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER
                )"""
            )
            self.db.execute(
                """CREATE INDEX IF NOT EXISTS scheduled_runs_ready
                ON scheduled_runs (status, sequence)"""
            )
            self.db.execute(
                """CREATE INDEX IF NOT EXISTS scheduled_runs_running
                ON scheduled_runs (status, repository_key)"""
            )

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _decode(row: sqlite3.Row) -> QueuedRun:
        return QueuedRun(
            row["run_id"],
            row["repository_key"],
            TaskSnapshot(**json.loads(row["snapshot_json"])),
            row["status"],
            bool(row["cancel_requested"]),
            bool(row["resource_held"]),
        )

    def get(self, run_id: str) -> QueuedRun | None:
        row = self.db.execute("SELECT * FROM scheduled_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._decode(row) if row is not None else None

    def interrupted(self) -> list[QueuedRun]:
        """List runs whose capacity is held until orphan cleanup is confirmed."""
        rows = self.db.execute(
            """SELECT * FROM scheduled_runs WHERE status = 'needs_review'
            AND resource_held = 1 ORDER BY sequence"""
        ).fetchall()
        return [self._decode(row) for row in rows]

    def enqueue(self, claim: IssueClaim) -> QueuedRun:
        """Queue one immutable claim once, keeping retries as distinct generations."""
        if claim.status != "claimed":
            raise ValueError("Only active issue claims can be queued")
        snapshot = claim.snapshot
        repository_key = f"{snapshot.installation_id}:{snapshot.repository_id}"
        encoded = json.dumps(asdict(snapshot), sort_keys=True)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                """INSERT INTO scheduled_runs
                (run_id, repository_key, snapshot_json, status, created_at)
                VALUES (?, ?, ?, 'queued', ?) ON CONFLICT (run_id) DO NOTHING""",
                (snapshot.run_id, repository_key, encoded, int(time.time())),
            )
            row = self.db.execute(
                "SELECT * FROM scheduled_runs WHERE run_id = ?", (snapshot.run_id,)
            ).fetchone()
            if row["repository_key"] != repository_key or row["snapshot_json"] != encoded:
                raise ValueError("Run ID belongs to a different issue snapshot")
        return self._decode(row)

    def dispatch(self) -> QueuedRun | None:
        """Atomically claim the earliest queued run whose repository has capacity."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            running = self.db.execute(
                "SELECT repository_key, COUNT(*) AS count FROM scheduled_runs "
                "WHERE resource_held = 1 GROUP BY repository_key"
            ).fetchall()
            occupancy = {row["repository_key"]: row["count"] for row in running}
            if sum(occupancy.values()) >= self.max_running:
                return None
            candidates = self.db.execute(
                "SELECT * FROM scheduled_runs WHERE status = 'queued' ORDER BY sequence"
            ).fetchall()
            row = next(
                (
                    item
                    for item in candidates
                    if occupancy.get(item["repository_key"], 0) < self.max_per_repository
                ),
                None,
            )
            if row is None:
                return None
            self.db.execute(
                """UPDATE scheduled_runs SET status = 'running', resource_held = 1,
                started_at = ? WHERE run_id = ?""",
                (int(time.time()), row["run_id"]),
            )
            return self.get(row["run_id"])

    def request_cancel(self, run_id: str, reason: str) -> QueuedRun:
        if not reason.strip():
            raise ValueError("Cancellation reason is required")
        with sqlite3.connect(self.path, timeout=30) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM scheduled_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown scheduled run")
            if row["status"] == "queued":
                connection.execute(
                    """UPDATE scheduled_runs SET status = 'cancelled', cancel_requested = 1,
                    cancel_reason = ?, result_json = ?, finished_at = ? WHERE run_id = ?""",
                    (reason, json.dumps({"cancel_reason": reason}), int(time.time()), run_id),
                )
            elif row["status"] == "running":
                connection.execute(
                    """UPDATE scheduled_runs SET cancel_requested = 1, cancel_reason = ?
                    WHERE run_id = ?""",
                    (reason, run_id),
                )
            updated = connection.execute(
                "SELECT * FROM scheduled_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._decode(updated)

    def is_cancel_requested(self, run_id: str) -> bool:
        """Read a cancellation signal from any execution thread."""
        with sqlite3.connect(self.path, timeout=30) as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM scheduled_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return bool(row and row[0])

    def finish(self, run_id: str, status: str, result: dict) -> QueuedRun:
        if status not in FINAL_STATUSES:
            raise ValueError("Invalid scheduled run outcome")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM scheduled_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or row["status"] != "running":
                raise ValueError("Run is not currently dispatched")
            if row["cancel_requested"]:
                status = "cancelled"
                result = {**result, "cancel_reason": row["cancel_reason"]}
            self.db.execute(
                """UPDATE scheduled_runs SET status = ?, resource_held = 0,
                result_json = ?, finished_at = ?
                WHERE run_id = ?""",
                (status, json.dumps(result), int(time.time()), run_id),
            )
        return self.get(run_id)

    def result(self, run_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT result_json FROM scheduled_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return json.loads(row[0]) if row is not None and row[0] else None

    def recover_interrupted(self) -> list[str]:
        """Quarantine uncertain work while retaining its resource reservation."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT run_id FROM scheduled_runs WHERE status = 'running' ORDER BY sequence"
            ).fetchall()
            for row in rows:
                self.db.execute(
                    """UPDATE scheduled_runs SET status = 'needs_review',
                    result_json = ?, finished_at = ? WHERE run_id = ?""",
                    (
                        json.dumps({"reason": "Controller stopped while run was active"}),
                        int(time.time()),
                        row["run_id"],
                    ),
                )
        return [row["run_id"] for row in rows]

    def acknowledge_stopped(self, run_id: str) -> None:
        """Release an interrupted run's slot only after its process/container is stopped."""
        with self.db:
            cursor = self.db.execute(
                """UPDATE scheduled_runs SET resource_held = 0
                WHERE run_id = ? AND status = 'needs_review' AND resource_held = 1""",
                (run_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("Run has no interrupted resource reservation")


def run_queued(
    queue: RunQueue,
    execute: Callable[[QueuedRun, Callable[[], bool]], dict],
    *,
    lock_path: Path,
) -> list[QueuedRun]:
    """Run queued tasks concurrently under one controller lock and persisted limits."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Scheduler is active in another controller") from exc
        queue.recover_interrupted()
        completed: list[QueuedRun] = []
        with ThreadPoolExecutor(max_workers=queue.max_running) as executor:
            active: dict[Future[dict], QueuedRun] = {}
            while True:
                while len(active) < queue.max_running:
                    item = queue.dispatch()
                    if item is None:
                        break
                    future = executor.submit(
                        execute, item, lambda run_id=item.run_id: queue.is_cancel_requested(run_id)
                    )
                    active[future] = item
                if not active:
                    break
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    item = active.pop(future)
                    try:
                        result = future.result()
                        status = result.get("status", "completed")
                        if status not in FINAL_STATUSES:
                            status = "completed"
                    except CancelledError:
                        status, result = "cancelled", {"reason": "Execution cancelled"}
                    except Exception as exc:
                        status = "failed"
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                    completed.append(queue.finish(item.run_id, status, result))
        return completed
