"""Review saved benchmark candidates with a separate local model."""

from __future__ import annotations

import json
import os
from pathlib import Path

import fire

from .review import OpenAICompatibleReviewer
from .review_eval import review_benchmark_output


def run(output_dir: str, model: str | None = None, base_url: str | None = None) -> None:
    """Record review findings and their potential effect on benchmark decisions."""
    selected_model = model or os.getenv("ENGINEER_REVIEW_MODEL") or os.getenv("ENGINEER_MODEL")
    if not selected_model:
        raise SystemExit("Set --model, ENGINEER_REVIEW_MODEL, or ENGINEER_MODEL")
    endpoint = base_url or os.getenv("ENGINEER_BASE_URL", "http://127.0.0.1:11434/v1")
    reviewer = OpenAICompatibleReviewer(selected_model, endpoint)
    print(json.dumps(review_benchmark_output(Path(output_dir), reviewer), indent=2))


def main() -> None:
    fire.Fire(run, name="engineer-bench-review")
