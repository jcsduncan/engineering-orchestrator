from __future__ import annotations

import fcntl
import hashlib
import json
import re
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import BinaryIO

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
from .state import ACTIVE_STATES, FINAL_STATES, RunStore
from .validation import CheckResult, validate_worktree

__all__ = ["Plan", "plan_task", "run_task", "execute_task"]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _validate_plan(plan: Plan) -> None:
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


def _provider_metadata(planner: PlanningProvider, worker: WorkerProvider) -> dict:
    planner_info, worker_info = planner.info, worker.info
    if not isinstance(planner_info, ProviderInfo) or not isinstance(worker_info, ProviderInfo):
        raise ValueError("Provider returned invalid ProviderInfo")
    try:
        providers = {"planner": asdict(planner_info), "worker": asdict(worker_info)}
        json.dumps(providers)
    except (TypeError, ValueError) as exc:
        raise ValueError("Provider metadata is not JSON-serializable") from exc
    return providers


def _run_id(value: str | None) -> str:
    """Return a safe run ID for branch and state paths."""
    run_id = value if value is not None else uuid.uuid4().hex[:12]
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise ValueError("Run ID must be 1-64 letters, digits, hyphens, or underscores")
    return run_id


def _acquire_run_lock(state_dir: Path, run_id: str) -> BinaryIO:
    """Claim one run without waiting for another controller."""
    lock_path = state_dir / "locks" / f"{run_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(f"Run {run_id} is active in another controller") from exc
    return handle


def _ensure_worktree(repo: Path, worktree: Path, branch: str, base_sha: str) -> None:
    """Reuse or create the recorded clean worktree at its pinned commit."""
    if worktree.is_symlink():
        raise RuntimeError("Run worktree cannot be a symlink")
    if worktree.exists():
        try:
            root = Path(git(worktree, "rev-parse", "--show-toplevel")).resolve()
            actual_branch = git(worktree, "symbolic-ref", "--short", "HEAD")
            actual_sha = git(worktree, "rev-parse", "HEAD")
            dirty = git(worktree, "status", "--porcelain")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("Existing run worktree is not a valid Git worktree") from exc
        if root != worktree.resolve() or actual_branch != branch or actual_sha != base_sha or dirty:
            raise RuntimeError("Existing run worktree does not match its recorded base")
        return
    try:
        branch_sha = git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
    except subprocess.CalledProcessError:
        branch_sha = None
    if branch_sha is not None and branch_sha != base_sha:
        raise RuntimeError("Existing run branch does not match its recorded base")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if branch_sha is None:
        git(repo, "worktree", "add", "-b", branch, str(worktree), base_sha)
    else:
        registrations = git(repo, "worktree", "list", "--porcelain").split("\n\n")
        for registration in registrations:
            lines = registration.splitlines()
            if f"branch refs/heads/{branch}" in lines:
                registered = next(
                    line.removeprefix("worktree ") for line in lines if line.startswith("worktree ")
                )
                if Path(registered).resolve() != worktree.resolve():
                    raise RuntimeError("Run branch belongs to another worktree")
        git(repo, "worktree", "add", "--force", str(worktree), branch)


def _interrupted_result(store: RunStore, record: dict) -> dict:
    """Preserve available evidence without repeating an uncertain call."""
    result = record["result"] or {}
    result.update(
        {
            "id": record["id"],
            "branch": record["branch"],
            "worktree": record["worktree"],
            "base_sha": record["base_sha"],
            "plan": record["plan"],
            "providers": record["providers"],
            "status": "needs_review",
            "reason": f"Interrupted during {record['status']}; commands were not repeated",
        }
    )
    worktree = Path(record["worktree"])
    if record["status"] == "planning":
        store.transition(record["id"], "planning", "needs_review", result=result)
        return result
    if worktree.is_symlink():
        result["worktree_error"] = "Run worktree is a symlink"
    elif worktree.exists():
        try:
            result["changed_files"] = git(worktree, "status", "--short").splitlines()
        except subprocess.CalledProcessError:
            result["worktree_error"] = "Run worktree cannot be inspected"
    else:
        result["worktree_error"] = "Run worktree is missing"
    store.transition(record["id"], record["status"], "needs_review", result=result)
    return result


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
    run_id: str | None = None,
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
        run_id=run_id,
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
    run_id: str | None = None,
) -> dict:
    """Execute or safely resume a task with independently selected providers."""
    repo = Path(git(repo, "rev-parse", "--show-toplevel"))
    state_dir = state_dir.expanduser().resolve()
    if type(check_timeout) is not int or check_timeout <= 0:
        raise ValueError("Check timeout must be positive")
    providers = _provider_metadata(planner, worker)
    request = {
        "task": task,
        "repo": str(repo),
        "providers": providers,
        "checks": checks,
        "check_timeout": check_timeout,
    }
    request_digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    run_id = _run_id(run_id)
    lock = _acquire_run_lock(state_dir, run_id)
    try:
        store = RunStore(state_dir / "runs.sqlite3")
    except Exception:
        lock.close()
        raise
    owned = False
    try:
        record = store.get(run_id)
        if record is None:
            if git(repo, "status", "--porcelain"):
                raise ValueError("Target repository must have a clean worktree")
            base_sha = git(repo, "rev-parse", "HEAD")
            branch = f"agent/{run_id}"
            worktree = state_dir / "worktrees" / run_id
            store.create(
                run_id=run_id,
                repo=repo,
                base_sha=base_sha,
                branch=branch,
                worktree=worktree,
                task=task,
                snapshot={**request, "request_digest": request_digest, "base_sha": base_sha},
                providers=providers,
            )
            record = store.get(run_id)
        elif record["snapshot"].get("request_digest") != request_digest:
            if (
                record["snapshot"]
                or record["task"] != task
                or record["repo"] != str(repo)
                or record["status"] not in FINAL_STATES | {"running"}
            ):
                raise ValueError("Run ID belongs to a different task or configuration")
            if record["status"] == "running":
                owned = True
                return _interrupted_result(store, record)
        if record["status"] in FINAL_STATES:
            return record["result"] or {"id": run_id, "status": record["status"]}
        owned = True
        if record["status"] in {"planning", "executing", "validating"}:
            return _interrupted_result(store, record)
        base_sha = record["base_sha"]
        branch = record["branch"]
        worktree = Path(record["worktree"])
        if record["status"] == "accepted":
            store.transition(run_id, "accepted", "planning")
            record = store.get(run_id)
        if record["status"] == "planning":
            plan = planner.plan(task)
            _validate_plan(plan)
            store.transition(run_id, "planning", "workspace_pending", plan=asdict(plan))
            record = store.get(run_id)
        plan = Plan(**record["plan"])
        _ensure_worktree(repo, worktree, branch, base_sha)
        store.transition(run_id, "workspace_pending", "executing")
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
        progress = {
            "id": run_id,
            "branch": branch,
            "worktree": str(worktree),
            "base_sha": base_sha,
            "plan": asdict(plan),
            "providers": providers,
            "worker": asdict(outcome),
            "checks": [],
        }
        store.transition(run_id, "executing", "validating", result=progress)

        def save_check(item: CheckResult) -> None:
            progress["checks"].append(item)
            store.save_progress(run_id, "validating", progress)

        validation = validate_worktree(
            worktree,
            checks if outcome.exit_code == 0 else [],
            check_timeout,
            on_check=save_check,
        )
        progress["validation"] = {
            "diff_sha256_before": validation.diff_sha256_before,
            "diff_sha256_after": validation.diff_sha256_after,
            "status": validation.status,
            "reason": validation.reason,
        }
        diff = git(worktree, "status", "--short")
        result = {**progress, "changed_files": diff.splitlines()}
        status = (
            "ready_for_review"
            if (outcome.exit_code == 0 and validation.status == "passed" and bool(diff))
            else "needs_review"
        )
        result["status"] = status
        store.transition(run_id, "validating", status, result=result)
        return result
    except Exception as exc:
        record = store.get(run_id)
        if owned and record is not None and record["status"] in ACTIVE_STATES:
            store.transition(
                run_id,
                record["status"],
                "failed",
                result={**(record["result"] or {}), "error": str(exc)},
            )
        raise
    finally:
        store.close()
        lock.close()
