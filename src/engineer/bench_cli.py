"""Command line entry point for the reproducible benchmark suite."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import fire

from .benchmarks import run_suite


def run(
    manifest: str,
    output_dir: str,
    worker: str,
    image: str,
    model: str | None = None,
    base_url: str | None = None,
    interventions: str | None = None,
    costs_usd: str | None = None,
    container_network: str = "none",
) -> None:
    """Run a suite with a local planner and coding worker.

    Intervention and cost maps are JSON objects keyed by case ID. Missing values stay
    unmeasured; the benchmark does not guess model billing or human involvement.
    """
    model = model or os.getenv("ENGINEER_MODEL")
    if not model:
        raise SystemExit("Set --model or ENGINEER_MODEL")
    summary = run_suite(
        manifest=Path(manifest),
        output_dir=Path(output_dir),
        model=model,
        base_url=base_url or os.getenv("ENGINEER_BASE_URL", "http://127.0.0.1:11434/v1"),
        worker=shlex.split(worker),
        image=image,
        container_network=container_network,
        interventions=json.loads(interventions) if interventions else None,
        costs_usd=json.loads(costs_usd) if costs_usd else None,
    )
    print(json.dumps(summary, indent=2))


def main() -> None:
    fire.Fire(run, name="engineer-bench")
