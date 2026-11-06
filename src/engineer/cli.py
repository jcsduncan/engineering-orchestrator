from __future__ import annotations

import json
import os
import shlex
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
) -> None:
    """Plan a task locally, or execute it with a coding worker and validation commands.

    Pass multiple checks as a list, for example --check='["ruff check .", "pytest -q"]'.
    """
    model = model or os.getenv("ENGINEER_MODEL")
    if not model:
        raise SystemExit("Set --model or ENGINEER_MODEL")
    base_url = base_url or os.getenv("ENGINEER_BASE_URL", "http://127.0.0.1:11434/v1")
    if worker:
        checks = [check] if isinstance(check, str) else (check or [])
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
        )
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(plan_task(task, model, base_url).__dict__, indent=2))


def main() -> None:
    fire.Fire(run, name="engineer")


if __name__ == "__main__":
    main()
