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

from .container import ContainerCheckRunner, ContainerRuntime, ContainerWorker
from .providers import (
    CheckRunner,
    CommandWorker,
    OpenAICompatiblePlanner,
    Plan,
    PlanningProvider,
    ProviderInfo,
    WorkerProvider,
    WorkerResult,
    plan_task,
)
from .reports import REPORT_PREFIX, parse_investigation_report
from .state import ACTIVE_STATES, FINAL_STATES, RunStore
from .validation import CheckResult, validate_worktree

__all__ = ["Plan", "plan_task", "run_task", "execute_task"]


def git(repo: Path, *args: str, git_dir: Path | None = None) -> str:
    command = ["git", "-C", str(repo)]
    if git_dir is not None:
        command = [
            "git",
            f"--git-dir={git_dir}",
            f"--work-tree={repo}",
            "-c",
            "core.fsmonitor=false",
        ]
    result = subprocess.run([*command, *args], capture_output=True, text=True, check=True)
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


def _registered_git_dir(repo: Path, worktree: Path) -> Path | None:
    """Check a worktree pointer against metadata owned by the target repository."""
    pointer = worktree / ".git"
    if pointer.is_symlink() or not pointer.is_file():
        return None
    try:
        content = pointer.read_text().strip()
        if not content.startswith("gitdir: "):
            return None
        target = Path(content.removeprefix("gitdir: "))
        if not target.is_absolute():
            target = worktree / target
        target = target.resolve(strict=True)
        common_dir = Path(
            git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve(strict=True)
        if not target.is_relative_to(common_dir / "worktrees"):
            return None
        back_pointer = Path((target / "gitdir").read_text().strip()).resolve(strict=True)
        return target if back_pointer == pointer.resolve(strict=True) else None
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def _ensure_worktree(repo: Path, worktree: Path, branch: str, base_sha: str) -> None:
    """Reuse or create the recorded clean worktree at its pinned commit."""
    if worktree.is_symlink():
        raise RuntimeError("Run worktree cannot be a symlink")
    if worktree.exists():
        git_dir = _registered_git_dir(repo, worktree)
        if git_dir is None:
            raise RuntimeError("Existing run worktree has an invalid Git metadata pointer")
        try:
            root = Path(git(worktree, "rev-parse", "--show-toplevel", git_dir=git_dir)).resolve()
            actual_branch = git(worktree, "symbolic-ref", "--short", "HEAD", git_dir=git_dir)
            actual_sha = git(worktree, "rev-parse", "HEAD", git_dir=git_dir)
            dirty = git(worktree, "status", "--porcelain", git_dir=git_dir)
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
            "mode": record["mode"],
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
        git_dir = _registered_git_dir(Path(record["repo"]), worktree)
        if git_dir is None:
            result["worktree_error"] = "Run worktree Git metadata pointer is invalid"
            store.transition(record["id"], record["status"], "needs_review", result=result)
            return result
        try:
            changed_files = git(worktree, "status", "--short", git_dir=git_dir)
            result["changed_files"] = changed_files.splitlines()
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
    execution_image: str | None = None,
    container_cpus: int = 2,
    container_memory_mb: int = 2048,
    container_pids: int = 128,
    container_network: str = "none",
    mode: str = "implementation",
) -> dict:
    """Execute with the default planner and a host or container command worker."""
    if execution_image:
        git_metadata = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        runtime = ContainerRuntime(
            execution_image,
            git_metadata,
            cpus=container_cpus,
            memory_mb=container_memory_mb,
            pids=container_pids,
            network=container_network,
        ).resolve_image()
        worker_provider: WorkerProvider = ContainerWorker(runtime, tuple(worker), worker_timeout)
        check_runner: CheckRunner | None = ContainerCheckRunner(runtime)
    else:
        worker_provider = CommandWorker(tuple(worker), worker_timeout)
        check_runner = None
    return execute_task(
        task=task,
        repo=repo,
        planner=OpenAICompatiblePlanner(model, base_url),
        worker=worker_provider,
        checks=checks,
        state_dir=state_dir,
        check_timeout=check_timeout,
        run_id=run_id,
        check_runner=check_runner,
        mode=mode,
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
    check_runner: CheckRunner | None = None,
    mode: str = "implementation",
) -> dict:
    """Execute or safely resume a task with independently selected providers."""
    if not isinstance(mode, str) or mode not in {"implementation", "investigation"}:
        raise ValueError("Mode must be implementation or investigation")
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
    if mode != "implementation":
        request["mode"] = mode
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
                mode=mode,
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
            planning_task = (
                task
                if mode == "implementation"
                else f"Investigate without changing repository files. Task: {task}"
            )
            plan = planner.plan(planning_task)
            _validate_plan(plan)
            store.transition(run_id, "planning", "workspace_pending", plan=asdict(plan))
            record = store.get(run_id)
        plan = Plan(**record["plan"])
        _ensure_worktree(repo, worktree, branch, base_sha)
        git_dir = _registered_git_dir(repo, worktree)
        if git_dir is None:
            raise RuntimeError("Run worktree has an invalid Git metadata pointer")
        git_pointer = (worktree / ".git").read_bytes()

        def verify_git_pointer() -> None:
            pointer = worktree / ".git"
            if pointer.is_symlink() or not pointer.is_file() or pointer.read_bytes() != git_pointer:
                raise ValueError("Worker changed Git worktree metadata pointer")

        store.transition(run_id, "workspace_pending", "executing")
        prompt = (
            f"Task:\n{task}\n\nPlan from the local controller (verify against code):\n"
            f"{json.dumps(asdict(plan), indent=2)}\n\n"
        )
        if mode == "investigation":
            prompt += (
                "Investigate this task without changing files. Run relevant diagnostic commands. "
                "Finish with exactly one line beginning "
                f"{REPORT_PREFIX} followed by a JSON object with: "
                "outcome ('substantiated' or 'inconclusive'), summary (string), "
                "findings (array of {claim: string, evidence: nonempty array of strings}), "
                "commands (array of {argv: nonempty array of strings, result: string}), "
                "and uncertainty (array of strings). Cite concrete file locations or command "
                "results in evidence. If the cause cannot be established, use 'inconclusive' "
                "and explain what evidence is missing. Report only commands you actually ran. "
                "Do not commit, push, or open a pull request."
            )
        else:
            prompt += (
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
            "mode": mode,
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
            runner=check_runner,
            run_id=run_id,
            git_guard=verify_git_pointer,
            git_dir=git_dir,
        )
        progress["validation"] = {
            "diff_sha256_before": validation.diff_sha256_before,
            "diff_sha256_after": validation.diff_sha256_after,
            "status": validation.status,
            "reason": validation.reason,
        }
        verify_git_pointer()
        diff = git(worktree, "status", "--short", git_dir=git_dir)
        result = {**progress, "changed_files": diff.splitlines()}
        if mode == "investigation":
            report = None
            failure_reason = None
            if outcome.exit_code != 0:
                failure_reason = "Worker did not exit successfully"
            else:
                try:
                    report = parse_investigation_report(outcome.stdout)
                except ValueError as exc:
                    failure_reason = str(exc)
            if failure_reason is None and diff:
                failure_reason = "Investigation changed the worktree"
            elif failure_reason is None and validation.status not in {"passed", "not_run"}:
                detail = validation.reason or "check failed"
                failure_reason = f"Validation {validation.status}: {detail}"
            result["report"] = report
            if failure_reason:
                result["failure_reason"] = failure_reason
                status = "failed"
            elif report["outcome"] == "substantiated":
                status = "report_for_review"
            else:
                status = "inconclusive"
        else:
            status = (
                "ready_for_review"
                if outcome.exit_code == 0 and validation.status == "passed" and diff
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
