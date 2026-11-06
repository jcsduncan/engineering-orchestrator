from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import fire

from .core import cancel_run, plan_task, run_task, status_run
from .review import OpenAICompatibleReviewer, review_run


def _state_dir(value: str | None) -> Path:
    return Path(value) if value else Path.home() / ".local/share/engineering-orchestrator"


def run(
    task: str,
    repo: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    worker: str | None = None,
    check: str | list[str] | None = None,
    state_dir: str | None = None,
    worker_timeout: int = 1800,
    run_id: str | None = None,
    image: str | None = None,
    container_cpus: int = 2,
    container_memory_mb: int = 2048,
    container_pids: int = 128,
    container_network: str = "none",
    mode: str = "implementation",
    retry_of: str | None = None,
) -> None:
    """Plan a task locally, or execute it with a coding worker and validation commands.

    Pass multiple checks as a list, for example --check='["ruff check .", "pytest -q"]'.
    """
    if not isinstance(mode, str) or mode not in {"implementation", "investigation"}:
        raise SystemExit("--mode must be implementation or investigation")
    model = model or os.getenv("ENGINEER_MODEL")
    if not model:
        raise SystemExit("Set --model or ENGINEER_MODEL")
    base_url = base_url or os.getenv("ENGINEER_BASE_URL", "http://127.0.0.1:11434/v1")
    if run_id is not None and not worker:
        raise SystemExit("--run-id requires --worker")
    if retry_of is not None and not worker:
        raise SystemExit("--retry-of requires --worker")
    if run_id is not None:
        run_id = str(run_id)
    if worker:
        if check is None:
            checks = []
        elif isinstance(check, str):
            checks = [check]
        elif isinstance(check, list) and all(isinstance(item, str) for item in check):
            checks = check
        else:
            raise SystemExit("--check must be a command or a list of commands")
        if not image:
            raise SystemExit("Set --image to a local worker image for container execution")
        result = run_task(
            task=task,
            repo=Path(repo) if repo else Path.cwd(),
            model=model,
            base_url=base_url,
            worker=shlex.split(worker),
            checks=checks,
            state_dir=_state_dir(state_dir),
            worker_timeout=worker_timeout,
            run_id=run_id,
            execution_image=image,
            container_cpus=container_cpus,
            container_memory_mb=container_memory_mb,
            container_pids=container_pids,
            container_network=container_network,
            mode=mode,
            retry_of=retry_of,
        )
        print(json.dumps(result, indent=2))
    else:
        planning_task = (
            task
            if mode == "implementation"
            else f"Investigate without changing repository files. Task: {task}"
        )
        print(json.dumps(plan_task(planning_task, model, base_url).__dict__, indent=2))


def status(run_id: str, state_dir: str | None = None) -> None:
    """Show a run's status and available evidence."""
    print(json.dumps(status_run(_state_dir(state_dir), str(run_id)), indent=2))


def cancel(run_id: str, reason: str, state_dir: str | None = None) -> None:
    """Stop an active run with a recorded reason."""
    print(json.dumps(cancel_run(_state_dir(state_dir), str(run_id), str(reason)), indent=2))


def retry(source_run_id: str, task: str, **options) -> None:
    """Run a new generation of a failed or cancelled task."""
    run(task, retry_of=str(source_run_id), **options)


def review(
    run_id: str,
    state_dir: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> None:
    """Ask an independent local model to review a validated change."""
    selected_model = model or os.getenv("ENGINEER_REVIEW_MODEL") or os.getenv("ENGINEER_MODEL")
    if not selected_model:
        raise SystemExit("Set --model, ENGINEER_REVIEW_MODEL, or ENGINEER_MODEL")
    endpoint = base_url or os.getenv("ENGINEER_BASE_URL", "http://127.0.0.1:11434/v1")
    reviewer = OpenAICompatibleReviewer(selected_model, endpoint)
    print(json.dumps(review_run(_state_dir(state_dir), str(run_id), reviewer), indent=2))


def main() -> None:
    """Expose the task command without silently dropping repeated validation flags."""
    check_flags = sum(
        arg.startswith("-") and arg.lstrip("-").split("=", 1)[0] in {"c", "check"}
        for arg in sys.argv[1:]
    )
    if check_flags > 1:
        raise SystemExit('Pass multiple checks as one list: --check=\'["cmd1", "cmd2"]\'')
    if len(sys.argv) > 1 and sys.argv[1] in {"status", "cancel", "retry", "review"}:
        fire.Fire(
            {"status": status, "cancel": cancel, "retry": retry, "review": review},
            name="engineer",
        )
    else:
        fire.Fire(run, name="engineer")


if __name__ == "__main__":
    main()
