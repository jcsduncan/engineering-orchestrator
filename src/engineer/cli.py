from __future__ import annotations

import argparse
import json
import os
import shlex
from pathlib import Path

from .core import plan_task, run_task


def main() -> None:
    parser = argparse.ArgumentParser(description="Local-model engineering task controller")
    parser.add_argument("task", help="Task description")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--model", default=os.getenv("ENGINEER_MODEL"))
    parser.add_argument(
        "--base-url",
        default=os.getenv("ENGINEER_BASE_URL", "http://127.0.0.1:11434/v1"),
    )
    parser.add_argument(
        "--worker", help="Coding CLI prefix, for example 'qwen --approval-mode auto -p'"
    )
    parser.add_argument(
        "--check",
        action="append",
        default=[],
        help="Validation command; repeat as needed",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".local/share/engineering-orchestrator",
    )
    parser.add_argument("--worker-timeout", type=int, default=1800)
    args = parser.parse_args()
    if not args.model:
        parser.error("Set --model or ENGINEER_MODEL")
    if args.worker:
        result = run_task(
            task=args.task,
            repo=args.repo,
            model=args.model,
            base_url=args.base_url,
            worker=shlex.split(args.worker),
            checks=args.check,
            state_dir=args.state_dir,
            worker_timeout=args.worker_timeout,
        )
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(plan_task(args.task, args.model, args.base_url).__dict__, indent=2))


if __name__ == "__main__":
    main()
