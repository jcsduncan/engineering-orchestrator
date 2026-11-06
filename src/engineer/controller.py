"""Connect durable issue claims to bounded engineering runs."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import subprocess
import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .capabilities import WorkerProfile, require_mode, verify_worker_image
from .core import run_task
from .intake import IssueClaimStore, RepositoryConfig, TaskSnapshot
from .providers import OpenAICompatiblePlanner
from .scheduler import QueuedRun, RunQueue, run_queued
from .state import ACTIVE_STATES, RunStore

RUN_OUTCOMES = {
    "ready_for_review": "completed",
    "report_for_review": "completed",
    "inconclusive": "completed",
    "needs_review": "needs_review",
    "failed": "failed",
    "cancelled": "cancelled",
}


@dataclass(frozen=True)
class RepositoryTarget:
    installation_id: int
    repository_id: int
    owner: str
    repository: str
    path: Path
    checks: tuple[str, ...]
    mode: str = "implementation"

    def __post_init__(self) -> None:
        RepositoryConfig(self.installation_id, self.owner, self.repository)
        if type(self.repository_id) is not int or self.repository_id <= 0:
            raise ValueError("repository_id must be a positive integer")
        if self.mode not in {"implementation", "investigation"}:
            raise ValueError("Mode must be implementation or investigation")
        if not self.path.is_absolute() or not self.path.is_dir():
            raise ValueError("Repository path must be an existing absolute directory")
        if not isinstance(self.checks, tuple) or any(
            not isinstance(check, str) or not check.strip() for check in self.checks
        ):
            raise ValueError("Checks must be nonempty command strings")
        if self.mode == "implementation" and not self.checks:
            raise ValueError("Implementation requires at least one validation command")

    @property
    def key(self) -> tuple[int, int]:
        return self.installation_id, self.repository_id

    def accepts(self, snapshot: TaskSnapshot) -> bool:
        return (
            self.key == (snapshot.installation_id, snapshot.repository_id)
            and self.owner == snapshot.owner
            and self.repository == snapshot.repository
        )


@dataclass(frozen=True)
class ControllerSettings:
    state_dir: Path
    targets: tuple[RepositoryTarget, ...]
    model: str
    base_url: str
    worker: tuple[str, ...]
    image: str
    max_running: int = 2
    max_per_repository: int = 1
    poll_interval: float = 1.0
    worker_timeout: int = 1800
    check_timeout: int = 300
    container_cpus: int = 2
    container_memory_mb: int = 2048
    container_pids: int = 128
    container_network: str = "none"
    worker_mode: str = "single"
    worker_profile: WorkerProfile | None = None

    def __post_init__(self) -> None:
        if not self.state_dir.is_absolute():
            raise ValueError("state_dir must be absolute")
        if not self.targets or len({target.key for target in self.targets}) != len(self.targets):
            raise ValueError("Configure at least one distinct repository identity")
        if len({target.path.resolve() for target in self.targets}) != len(self.targets):
            raise ValueError("Each repository needs a distinct local checkout")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.model, self.base_url, self.image)
        ):
            raise ValueError("Model, base URL, and image are required")
        OpenAICompatiblePlanner(self.model, self.base_url)
        if not self.worker or any(not isinstance(arg, str) or not arg for arg in self.worker):
            raise ValueError("Worker must be a nonempty argument vector")
        require_mode(self.worker_mode, self.worker_profile)
        if self.worker_profile is not None:
            self.worker_profile.require_command(self.worker)
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.max_running,
                self.max_per_repository,
                self.worker_timeout,
                self.check_timeout,
                self.container_cpus,
                self.container_memory_mb,
                self.container_pids,
            )
        ):
            raise ValueError("Limits and timeouts must be positive integers")
        if type(self.poll_interval) not in {int, float} or not 0.1 <= self.poll_interval <= 60:
            raise ValueError("poll_interval must be between 0.1 and 60 seconds")
        if self.container_network not in {"none", "bridge"}:
            raise ValueError("container_network must be none or bridge")

    def target_for(self, snapshot: TaskSnapshot) -> RepositoryTarget:
        target = next(
            (
                item
                for item in self.targets
                if item.key == (snapshot.installation_id, snapshot.repository_id)
            ),
            None,
        )
        if target is None or not target.accepts(snapshot):
            raise ValueError("Issue claim is outside configured repository scope")
        return target


def load_settings(path: Path) -> ControllerSettings:
    """Load an explicit, secret-free repository and worker allowlist."""
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Controller configuration needs version 1")
    repositories = payload.get("repositories")
    worker = payload.get("worker")
    if not isinstance(repositories, list) or not isinstance(worker, list):
        raise ValueError("Controller configuration needs repositories and worker argv")
    targets = []
    for item in repositories:
        if not isinstance(item, dict) or not isinstance(item.get("checks"), list):
            raise ValueError("Invalid repository configuration")
        targets.append(
            RepositoryTarget(
                item["installation_id"],
                item["repository_id"],
                item["owner"],
                item["repository"],
                Path(item["path"]),
                tuple(item["checks"]),
                item.get("mode", "implementation"),
            )
        )
    return ControllerSettings(
        state_dir=Path(payload["state_dir"]),
        targets=tuple(targets),
        model=payload["model"],
        base_url=payload["base_url"],
        worker=tuple(worker),
        image=payload["image"],
        worker_mode=payload.get("worker_mode", "single"),
        worker_profile=(
            WorkerProfile.from_dict(payload["worker_profile"])
            if "worker_profile" in payload
            else None
        ),
        max_running=payload.get("max_running", 2),
        max_per_repository=payload.get("max_per_repository", 1),
        poll_interval=payload.get("poll_interval", 1.0),
        worker_timeout=payload.get("worker_timeout", 1800),
        check_timeout=payload.get("check_timeout", 300),
        container_cpus=payload.get("container_cpus", 2),
        container_memory_mb=payload.get("container_memory_mb", 2048),
        container_pids=payload.get("container_pids", 128),
        container_network=payload.get("container_network", "none"),
    )


def claim_cancel_reason(database: Path, run_id: str) -> str | None:
    """Read a revocation through a connection owned by the calling thread."""
    with closing(sqlite3.connect(database, timeout=1)) as connection:
        row = connection.execute(
            "SELECT status, reason FROM issue_claims WHERE run_id = ?", (run_id,)
        ).fetchone()
    return (row[1] or "Issue claim revoked") if row is not None and row[0] == "cancelled" else None


def task_text(snapshot: TaskSnapshot) -> str:
    """Render issue data without treating it as controller configuration."""
    parts = [snapshot.title, f"Objective:\n{snapshot.objective}"]
    if snapshot.acceptance_criteria:
        parts.append(f"Acceptance criteria:\n{snapshot.acceptance_criteria}")
    parts.append(f"Issue: {snapshot.issue_url}")
    return "\n\n".join(parts)


def verify_checkouts(settings: ControllerSettings) -> None:
    """Reject invalid or dirty base checkouts before accepting claims."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    for target in settings.targets:

        def git(*args: str, repo_path: Path = target.path) -> str:
            try:
                result = subprocess.run(
                    ["git", "-C", str(repo_path), *args],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=20,
                    env=environment,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError(f"Invalid configured checkout: {repo_path}") from exc
            return result.stdout.strip()

        if Path(git("rev-parse", "--show-toplevel")).resolve() != target.path.resolve():
            raise ValueError(f"Configured checkout is not its Git root: {target.path}")
        git("rev-parse", "HEAD")
        if git("status", "--porcelain"):
            raise ValueError(f"Configured checkout must be clean: {target.path}")


def transfer_claims(settings: ControllerSettings, queue: RunQueue, claims: IssueClaimStore) -> None:
    """Accept current claims and mirror revocations into the queue."""
    for target in settings.targets:
        for claim in claims.pending_claims(*target.key):
            settings.target_for(claim.snapshot)
            try:
                queue.accept_claim(claim)
            except ValueError:
                latest = claims.get(claim.snapshot.run_id)
                if latest is None or latest.status == "claimed":
                    raise
        for claim in claims.pending_cancellations(*target.key):
            item = queue.get(claim.snapshot.run_id)
            if item is None:
                claims.acknowledge_cancel(claim.snapshot.run_id)
            elif item.status in {"queued", "running"} and not item.cancel_requested:
                queue.request_cancel(claim.snapshot.run_id, claim.reason or "Issue claim revoked")


def finalize_claims(settings: ControllerSettings, queue: RunQueue, claims: IssueClaimStore) -> None:
    """Replay claim completion after queue outcome persistence."""
    pending_ids = {
        claim.snapshot.run_id
        for target in settings.targets
        for claim in claims.pending_cancellations(*target.key)
    }
    for item in queue.finalized():
        if not any(target.accepts(item.snapshot) for target in settings.targets):
            continue
        claim = claims.get(item.run_id)
        if claim is None:
            continue
        if claim.status == "claimed":
            try:
                claims.complete(item.run_id)
                continue
            except ValueError:
                claim = claims.get(item.run_id)
                if claim is None or claim.status != "cancelled":
                    raise
        if claim.status == "cancelled" and item.run_id in pending_ids:
            claims.acknowledge_cancel(item.run_id)


def verify_queue_scope(settings: ControllerSettings, queue: RunQueue) -> None:
    """Leave queued work untouched if this controller does not own its repository."""
    rows = queue.db.execute(
        "SELECT run_id FROM scheduled_runs WHERE status IN ('queued', 'running') "
        "OR resource_held = 1"
    ).fetchall()
    for row in rows:
        item = queue.get(row["run_id"])
        settings.target_for(item.snapshot)


def run_pass(
    settings: ControllerSettings,
    *,
    execute_run: Callable[..., dict] = run_task,
) -> list[QueuedRun]:
    """Consume current claims while bounded runs execute; persist all outcomes."""
    verify_checkouts(settings)
    if settings.worker_profile is not None:
        verify_worker_image(settings.image, settings.worker_profile)
    database = settings.state_dir / "runs.sqlite3"
    queue = RunQueue(
        database,
        max_running=settings.max_running,
        max_per_repository=settings.max_per_repository,
    )
    claims = IssueClaimStore(database)
    try:
        finalize_claims(settings, queue, claims)
        transfer_claims(settings, queue, claims)
        verify_queue_scope(settings, queue)
        stop = threading.Event()
        producer_errors: list[Exception] = []

        def produce() -> None:
            producer_queue = RunQueue(
                database,
                max_running=settings.max_running,
                max_per_repository=settings.max_per_repository,
            )
            producer_claims = IssueClaimStore(database)
            try:
                while not stop.wait(settings.poll_interval):
                    transfer_claims(settings, producer_queue, producer_claims)
            except Exception as exc:
                producer_errors.append(exc)
                stop.set()
            finally:
                producer_claims.close()
                producer_queue.close()

        producer = threading.Thread(target=produce, daemon=True)
        producer.start()
        targets = {target.key: target for target in settings.targets}

        def execute(item: QueuedRun, should_cancel: Callable[[], bool]) -> dict:
            target = targets.get((item.snapshot.installation_id, item.snapshot.repository_id))
            if target is None or not target.accepts(item.snapshot):
                raise ValueError("Queued run is outside configured repository scope")

            def cancelled(run_id: str) -> str | None:
                return queue.cancellation_reason(run_id) or claim_cancel_reason(database, run_id)

            reason = cancelled(item.run_id)
            if reason or should_cancel():
                return {"status": "cancelled", "reason": reason or "Issue claim revoked"}
            result = execute_run(
                task=task_text(item.snapshot),
                repo=target.path,
                model=settings.model,
                base_url=settings.base_url,
                worker=list(settings.worker),
                checks=list(target.checks),
                state_dir=settings.state_dir,
                worker_timeout=settings.worker_timeout,
                check_timeout=settings.check_timeout,
                run_id=item.run_id,
                execution_image=settings.image,
                container_cpus=settings.container_cpus,
                container_memory_mb=settings.container_memory_mb,
                container_pids=settings.container_pids,
                container_network=settings.container_network,
                mode=target.mode,
                external_cancel_reason=cancelled,
                worker_mode=settings.worker_mode,
                worker_profile=settings.worker_profile,
            )
            run_status = result.get("status")
            if run_status not in RUN_OUTCOMES:
                raise ValueError(f"Unexpected run outcome: {run_status!r}")
            return {"status": RUN_OUTCOMES[run_status], "run_status": run_status}

        try:
            completed = run_queued(
                queue,
                execute,
                lock_path=settings.state_dir / "locks" / "scheduler.lock",
                poll_interval=settings.poll_interval,
            )
        finally:
            stop.set()
            producer.join(timeout=max(2.0, settings.poll_interval + 1.0))
        if producer.is_alive():
            raise RuntimeError("Claim transfer did not stop")
        if producer_errors:
            raise RuntimeError("Claim transfer failed") from producer_errors[0]
        transfer_claims(settings, queue, claims)
        finalize_claims(settings, queue, claims)
        return completed
    finally:
        claims.close()
        queue.close()


def acknowledge_interrupted(settings: ControllerSettings, run_id: str) -> QueuedRun:
    """Release an interrupted slot only after confirming its container is gone."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise ValueError("Invalid run ID")
    lock_path = settings.state_dir / "locks" / "scheduler.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Scheduler is active in another controller") from exc
        database = settings.state_dir / "runs.sqlite3"
        queue = RunQueue(
            database,
            max_running=settings.max_running,
            max_per_repository=settings.max_per_repository,
        )
        claims = IssueClaimStore(database)
        runs = RunStore(database)
        try:
            item = queue.get(run_id)
            if item is None or item.status != "needs_review" or not item.resource_held:
                raise ValueError("Run has no interrupted resource reservation")
            settings.target_for(item.snapshot)
            try:
                containers = subprocess.run(
                    [
                        "docker",
                        "ps",
                        "-a",
                        "--filter",
                        f"label=engineering-orchestrator.run={run_id}",
                        "--format",
                        "{{.ID}}",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError("Cannot confirm interrupted container is stopped") from exc
            if containers:
                raise RuntimeError("Interrupted run still has a container")
            record = runs.get(run_id)
            if record is not None and record["status"] in ACTIVE_STATES:
                previous = record["status"]
                status = (
                    "cancelled"
                    if record["cancel_reason"]
                    else "needs_review"
                    if previous in {"planning", "executing", "validating"}
                    else "failed"
                )
                runs.transition(
                    run_id,
                    previous,
                    status,
                    result={
                        **(record["result"] or {}),
                        "id": run_id,
                        "status": status,
                        "reason": "Controller stopped before run completion",
                    },
                )
            queue.acknowledge_stopped(run_id)
            finalize_claims(settings, queue, claims)
            return queue.get(run_id)
        finally:
            runs.close()
            claims.close()
            queue.close()
