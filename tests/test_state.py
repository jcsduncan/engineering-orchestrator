import sqlite3

import pytest

from engineer.state import RunStore


def test_run_store_records_atomic_transitions(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    store.create(
        run_id="run-1",
        repo=tmp_path,
        base_sha="a" * 40,
        branch="agent/run-1",
        worktree=tmp_path / "worktrees/run-1",
        task="Do work",
        snapshot={"checks": ["pytest -q"]},
        providers={"planner": {"name": "local"}},
    )
    store.transition("run-1", "accepted", "planning")
    store.transition("run-1", "planning", "workspace_pending", plan={"steps": ["Inspect"]})
    store.transition("run-1", "workspace_pending", "executing")
    store.transition("run-1", "executing", "validating", result={"worker": {"exit_code": 0}})
    store.save_progress("run-1", "validating", {"checks": [{"exit_code": 0}]})
    store.transition(
        "run-1", "validating", "ready_for_review", result={"status": "ready_for_review"}
    )

    record = store.get("run-1")
    assert record["status"] == "ready_for_review"
    assert record["snapshot"] == {"checks": ["pytest -q"]}
    assert record["plan"] == {"steps": ["Inspect"]}
    assert record["result"] == {"status": "ready_for_review"}
    assert store.events("run-1") == [
        "accepted",
        "planning",
        "workspace_pending",
        "executing",
        "validating",
        "ready_for_review",
    ]
    with pytest.raises(RuntimeError, match="no longer in planning"):
        store.transition("run-1", "planning", "workspace_pending")
    with pytest.raises(sqlite3.IntegrityError):
        store.create(
            run_id="run-1",
            repo=tmp_path,
            base_sha="a" * 40,
            branch="agent/run-1",
            worktree=tmp_path / "worktrees/run-1",
            task="Do work",
            snapshot={},
            providers={},
        )
    store.close()


def test_run_store_migrates_existing_database(tmp_path):
    path = tmp_path / "runs.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE runs (
                id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, repo TEXT NOT NULL,
                base_sha TEXT NOT NULL, branch TEXT NOT NULL, worktree TEXT NOT NULL,
                status TEXT NOT NULL, task TEXT NOT NULL, plan_json TEXT NOT NULL,
                result_json TEXT
            )"""
        )
        db.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "old",
                1,
                str(tmp_path),
                "a" * 40,
                "agent/old",
                str(tmp_path / "old"),
                "failed",
                "Task",
                "{}",
                None,
            ),
        )
    store = RunStore(path)
    record = store.get("old")
    assert record["providers"] == {}
    assert record["snapshot"] == {}
    assert record["status"] == "failed"
    store.close()
