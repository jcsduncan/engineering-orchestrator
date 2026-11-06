from __future__ import annotations

import json
import sqlite3
import subprocess
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .providers import (
    CommandWorker,
    OpenAICompatiblePlanner,
    Plan,
    PlanningProvider,
    ProviderInfo,
    WorkerProvider,
    WorkerResult,
    plan_task,
)
from .validation import validate_worktree

__all__ = ["Plan", "plan_task", "run_task", "execute_task"]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, repo TEXT NOT NULL,
            base_sha TEXT NOT NULL, branch TEXT NOT NULL, worktree TEXT NOT NULL,
            status TEXT NOT NULL, task TEXT NOT NULL, plan_json TEXT NOT NULL,
            result_json TEXT, providers_json TEXT NOT NULL DEFAULT '{}'
        )"""
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
    if "providers_json" not in columns:
        db.execute("ALTER TABLE runs ADD COLUMN providers_json TEXT NOT NULL DEFAULT '{}'")
    db.commit()
    return db


def _record(db: sqlite3.Connection, run_id: str, status: str, result: dict | None = None) -> None:
    db.execute(
        "UPDATE runs SET status = ?, result_json = ? WHERE id = ?",
        (status, json.dumps(result) if result is not None else None, run_id),
    )
    db.commit()


def run_task(
    *,
    task: str,
    repo: Path,
    model: str,
    base_url: str,
    worker: list[str],
    checks: list[str],
    state_dir: Path,
    worker_timeout: int = 1800,
    check_timeout: int = 300,
) -> dict:
    """Execute a task with the default local planner and command worker."""
    return execute_task(
        task=task,
        repo=repo,
        planner=OpenAICompatiblePlanner(model, base_url),
        worker=CommandWorker(tuple(worker), worker_timeout),
        checks=checks,
        state_dir=state_dir,
        check_timeout=check_timeout,
    )


def execute_task(
    *,
    task: str,
    repo: Path,
    planner: PlanningProvider,
    worker: WorkerProvider,
    checks: list[str],
    state_dir: Path,
    check_timeout: int = 300,
) -> dict:
    """Execute a task with independently selected planner and worker providers."""
    repo = Path(git(repo, "rev-parse", "--show-toplevel"))
    if git(repo, "status", "--porcelain"):
        raise ValueError("Target repository must have a clean worktree")
    base_sha = git(repo, "rev-parse", "HEAD")
    plan = planner.plan(task)
    if (
        not isinstance(plan, Plan)
        or not isinstance(plan.objective, str)
        or not plan.objective.strip()
        or any(
            not isinstance(items, list) or any(not isinstance(item, str) for item in items)
            for items in (plan.steps, plan.checks, plan.risks)
        )
    ):
        raise ValueError("Planning provider returned an invalid Plan")
    planner_info, worker_info = planner.info, worker.info
    if not isinstance(planner_info, ProviderInfo) or not isinstance(worker_info, ProviderInfo):
        raise ValueError("Provider returned invalid ProviderInfo")
    try:
        providers = {"planner": asdict(planner_info), "worker": asdict(worker_info)}
        providers_json = json.dumps(providers)
    except (TypeError, ValueError) as exc:
        raise ValueError("Provider metadata is not JSON-serializable") from exc
    run_id = uuid.uuid4().hex[:12]
    branch = f"agent/{run_id}"
    worktree = state_dir / "worktrees" / run_id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    db = _database(state_dir / "runs.sqlite3")
    try:
        git(repo, "worktree", "add", "-b", branch, str(worktree), base_sha)
        db.execute(
            """INSERT INTO runs
            (id, created_at, repo, base_sha, branch, worktree, status, task,
             plan_json, result_json, providers_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                int(time.time()),
                str(repo),
                base_sha,
                branch,
                str(worktree),
                "running",
                task,
                json.dumps(asdict(plan)),
                None,
                providers_json,
            ),
        )
        db.commit()
        prompt = (
            f"Task:\n{task}\n\nPlan from the local controller (verify against code):\n"
            f"{json.dumps(asdict(plan), indent=2)}\n\n"
            "Implement the task in this checkout. Run relevant tests. If the cause or "
            "solution is uncertain, report that clearly; do not invent a fix. "
            "Do not commit, push, or open a pull request."
        )
        outcome = worker.execute(prompt, worktree, run_id)
        if (
            not isinstance(outcome, WorkerResult)
            or (outcome.exit_code is not None and type(outcome.exit_code) is not int)
            or not isinstance(outcome.stdout, str)
            or not isinstance(outcome.stderr, str)
        ):
            raise ValueError("Worker provider returned an invalid WorkerResult")
        worker_result = asdict(outcome)
        validation = validate_worktree(
            worktree, checks if worker_result["exit_code"] == 0 else [], check_timeout
        )
        check_results = validation.checks
        diff = git(worktree, "status", "--short")
        result = {
            "id": run_id,
            "branch": branch,
            "worktree": str(worktree),
            "base_sha": base_sha,
            "plan": asdict(plan),
            "providers": providers,
            "worker": worker_result,
            "checks": check_results,
            "validation": {
                "diff_sha256_before": validation.diff_sha256_before,
                "diff_sha256_after": validation.diff_sha256_after,
                "status": validation.status,
                "reason": validation.reason,
            },
            "changed_files": diff.splitlines(),
        }
        status = (
            "ready_for_review"
            if (worker_result["exit_code"] == 0 and validation.status == "passed" and bool(diff))
            else "needs_review"
        )
        result["status"] = status
        _record(db, run_id, status, result)
        return result
    except Exception as exc:
        _record(db, run_id, "failed", {"error": str(exc)})
        raise
    finally:
        db.close()
