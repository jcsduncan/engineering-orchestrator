from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import fire

from .core import plan_task, run_task


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
) -> None:
    """Plan a task locally, or execute it with a coding worker and validation commands.

    Pass multiple checks as a list, for example --check='["ruff check .", "pytest -q"]'.
    """
    model = model or os.getenv("ENGINEER_MODEL")
    if not model:
        raise SystemExit("Set --model or ENGINEER_MODEL")
    base_url = base_url or os.getenv("ENGINEER_BASE_URL", "http://127.0.0.1:11434/v1")
    if run_id is not None and not worker:
        raise SystemExit("--run-id requires --worker")
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
            state_dir=Path(state_dir)
            if state_dir
            else Path.home() / ".local/share/engineering-orchestrator",
            worker_timeout=worker_timeout,
            run_id=run_id,
            execution_image=image,
            container_cpus=container_cpus,
            container_memory_mb=container_memory_mb,
            container_pids=container_pids,
            container_network=container_network,
        )
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(plan_task(task, model, base_url).__dict__, indent=2))


def main() -> None:
    """Expose the task command without silently dropping repeated validation flags."""
    check_flags = sum(
        arg.startswith("-") and arg.lstrip("-").split("=", 1)[0] in {"c", "check"}
        for arg in sys.argv[1:]
    )
    if check_flags > 1:
        raise SystemExit('Pass multiple checks as one list: --check=\'["cmd1", "cmd2"]\'')
    fire.Fire(run, name="engineer")


if __name__ == "__main__":
    main()
