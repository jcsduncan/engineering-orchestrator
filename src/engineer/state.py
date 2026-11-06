from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

ACTIVE_STATES = {"accepted", "planning", "workspace_pending", "executing", "validating"}
FINAL_STATES = {"ready_for_review", "needs_review", "failed"}
TRANSITIONS = {
    "running": {"needs_review", "failed"},
    "accepted": {"planning", "failed"},
    "planning": {"workspace_pending", "needs_review", "failed"},
    "workspace_pending": {"executing", "failed"},
    "executing": {"validating", "needs_review", "failed"},
    "validating": {"ready_for_review", "needs_review", "failed"},
}
_UNSET = object()


class RunStore:
    """Persist run checkpoints and transition events in one SQLite database."""

    def __init__(self, path: Path) -> None:
        """Open state storage and migrate older run records in place."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, repo TEXT NOT NULL,
                    base_sha TEXT NOT NULL, branch TEXT NOT NULL, worktree TEXT NOT NULL,
                    status TEXT NOT NULL, task TEXT NOT NULL, plan_json TEXT NOT NULL,
                    result_json TEXT, providers_json TEXT NOT NULL DEFAULT '{}',
                    snapshot_json TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}
            for column in ("providers_json", "snapshot_json"):
                if column not in columns:
                    self.db.execute(
                        f"ALTER TABLE runs ADD COLUMN {column} TEXT NOT NULL DEFAULT '{{}}'"
                    )
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    created_at INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}'
                )"""
            )

    def close(self) -> None:
        """Close the database connection."""
        self.db.close()

    def get(self, run_id: str) -> dict[str, Any] | None:
        """Load one run with decoded JSON fields."""
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        record = dict(row)
        for key in ("plan_json", "result_json", "providers_json", "snapshot_json"):
            raw = record.pop(key)
            record[key.removesuffix("_json")] = json.loads(raw) if raw else None
        return record

    def create(
        self,
        *,
        run_id: str,
        repo: Path,
        base_sha: str,
        branch: str,
        worktree: Path,
        task: str,
        snapshot: dict[str, Any],
        providers: dict[str, Any],
    ) -> None:
        """Create an accepted run and its first event atomically."""
        now = int(time.time())
        with self.db:
            self.db.execute(
                """INSERT INTO runs
                (id, created_at, repo, base_sha, branch, worktree, status, task,
                 plan_json, result_json, providers_json, snapshot_json)
                VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?, '{}', NULL, ?, ?)""",
                (
                    run_id,
                    now,
                    str(repo),
                    base_sha,
                    branch,
                    str(worktree),
                    task,
                    json.dumps(providers),
                    json.dumps(snapshot),
                ),
            )
            self.db.execute(
                "INSERT INTO run_events (run_id, created_at, status) VALUES (?, ?, 'accepted')",
                (run_id, now),
            )

    def transition(
        self,
        run_id: str,
        expected: str,
        status: str,
        *,
        plan: dict[str, Any] | object = _UNSET,
        result: dict[str, Any] | object = _UNSET,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Change state only when the recorded state matches the expected one."""
        if status not in TRANSITIONS.get(expected, set()):
            raise ValueError(f"Invalid run transition: {expected} -> {status}")
        assignments = ["status = ?"]
        values: list[Any] = [status]
        if plan is not _UNSET:
            assignments.append("plan_json = ?")
            values.append(json.dumps(plan))
        if result is not _UNSET:
            assignments.append("result_json = ?")
            values.append(json.dumps(result))
        values.extend((run_id, expected))
        with self.db:
            cursor = self.db.execute(
                f"UPDATE runs SET {', '.join(assignments)} WHERE id = ? AND status = ?", values
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Run {run_id} is no longer in {expected}")
            self.db.execute(
                """INSERT INTO run_events (run_id, created_at, status, detail_json)
                VALUES (?, ?, ?, ?)""",
                (run_id, int(time.time()), status, json.dumps(detail or {})),
            )

    def save_progress(self, run_id: str, status: str, result: dict[str, Any]) -> None:
        """Checkpoint partial evidence while a run stays in one state."""
        with self.db:
            cursor = self.db.execute(
                "UPDATE runs SET result_json = ? WHERE id = ? AND status = ?",
                (json.dumps(result), run_id, status),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Run {run_id} is no longer in {status}")

    def events(self, run_id: str) -> list[str]:
        """Return the persisted status history in event order."""
        return [
            row[0]
            for row in self.db.execute(
                "SELECT status FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)
            )
        ]
