"""Run bounded issue claims from a trusted controller configuration."""

from __future__ import annotations

import json
import time
from pathlib import Path

import fire

from .controller import acknowledge_interrupted, load_settings, run_pass
from .scheduler import RunQueue


def run(config: str, once: bool = False) -> None:
    """Consume configured issue claims, then repeat until interrupted unless --once is set."""
    settings = load_settings(Path(config))
    while True:
        completed = run_pass(settings)
        queue = RunQueue(
            settings.state_dir / "runs.sqlite3",
            max_running=settings.max_running,
            max_per_repository=settings.max_per_repository,
        )
        try:
            for item in completed:
                print(
                    json.dumps(
                        {
                            "run_id": item.run_id,
                            "status": item.status,
                            "result": queue.result(item.run_id),
                        }
                    )
                )
        finally:
            queue.close()
        if once:
            return
        time.sleep(settings.poll_interval)


def acknowledge(config: str, run_id: str) -> None:
    """Release an interrupted run after verifying its container is gone."""
    item = acknowledge_interrupted(load_settings(Path(config)), run_id)
    print(json.dumps({"run_id": item.run_id, "status": item.status}))


def main() -> None:
    fire.Fire({"run": run, "acknowledge": acknowledge}, name="engineer-controller")


if __name__ == "__main__":
    main()
