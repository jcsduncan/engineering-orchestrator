"""Run small, reproducible engineering tasks and retain their evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .core import run_task


def _digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _fixture_repo(source: Path, destination: Path) -> tuple[str, str]:
    if not source.is_dir() or any(part.startswith(".") for part in source.parts[-1:]):
        raise ValueError(f"Invalid fixture directory: {source}")
    digest = _digest_tree(source)
    shutil.copytree(source, destination)
    _git(destination, "init", "-q", "-b", "main")
    _git(destination, "add", ".")
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Benchmark Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Benchmark Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
    }
    subprocess.run(
        ["git", "-C", str(destination), "commit", "-qm", "Benchmark fixture"],
        check=True,
        env=environment,
    )
    return digest, _git(destination, "rev-parse", "HEAD")


def load_cases(manifest: Path) -> list[dict[str, Any]]:
    """Load and validate a versioned suite manifest."""
    payload = json.loads(manifest.read_text())
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Benchmark manifest must have version 1")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Benchmark manifest needs cases")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Benchmark case must be an object")
        name, fixture, task, mode = (
            case.get("id"),
            case.get("fixture"),
            case.get("task"),
            case.get("mode"),
        )
        if (
            not isinstance(name, str)
            or not name.isidentifier()
            or name in seen
            or not isinstance(fixture, str)
            or not fixture.isidentifier()
            or not isinstance(task, str)
            or not task.strip()
            or mode not in {"implementation", "investigation"}
        ):
            raise ValueError("Benchmark case has invalid identity, fixture, task, or mode")
        seen.add(name)
        for key in ("checks", "oracle"):
            value = case.get(key, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"Benchmark {name} has invalid {key}")
        if mode == "implementation" and not case.get("oracle"):
            raise ValueError(f"Benchmark {name} needs an implementation oracle")
        findings = case.get("required_findings", [])
        if not isinstance(findings, list) or any(
            not isinstance(item, str) or not item.strip() for item in findings
        ):
            raise ValueError(f"Benchmark {name} has invalid required findings")
        if mode == "investigation" and not findings:
            raise ValueError(f"Benchmark {name} needs required findings")
    return cases


def score_case(
    case: dict[str, Any],
    result: dict[str, Any],
    *,
    oracle_passed: bool | None,
    latency_seconds: float,
    interventions: int | None = None,
    cost_usd: float | None = None,
) -> dict[str, Any]:
    """Score correctness and evidence without counting inconclusive runs as failures."""
    status = result.get("status", "failed")
    validation = result.get("validation") or {}
    checks = result.get("checks") or []
    validation_quality = bool(
        checks
        and validation.get("status") == "passed"
        and validation.get("diff_sha256_before") == validation.get("diff_sha256_after")
        and all(item.get("status") == "passed" for item in checks)
    )
    if case["mode"] == "implementation":
        correctness = status == "ready_for_review" and oracle_passed is True
    else:
        report = result.get("report") or {}
        evidence = json.dumps(report.get("findings", []), sort_keys=True).lower()
        correctness = status == "report_for_review" and all(
            finding.lower() in evidence for finding in case["required_findings"]
        )
        validation_quality = bool(
            report.get("findings") and all(item.get("evidence") for item in report["findings"])
        )
    return {
        "outcome": "inconclusive"
        if status == "inconclusive"
        else "completed"
        if correctness
        else "failed",
        "correctness": correctness,
        "validation_quality": validation_quality,
        "interventions": interventions,
        "latency_seconds": round(latency_seconds, 3),
        "cost_usd": cost_usd,
    }


def run_suite(
    *,
    manifest: Path,
    output_dir: Path,
    model: str,
    base_url: str,
    worker: list[str],
    interventions: dict[str, int] | None = None,
    costs_usd: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Execute each fixture in a fresh repository and save complete run artifacts."""
    cases = load_cases(manifest)
    planner_origin = _origin(base_url)
    if not worker:
        raise ValueError("Benchmark worker command is required")
    known_ids = {case["id"] for case in cases}
    for values, label, expected in (
        (interventions or {}, "interventions", int),
        (costs_usd or {}, "costs_usd", (int, float)),
    ):
        if not isinstance(values, dict) or any(
            key not in known_ids
            or not isinstance(value, expected)
            or isinstance(value, bool)
            or value < 0
            or (isinstance(value, float) and not math.isfinite(value))
            for key, value in values.items()
        ):
            raise ValueError(f"Invalid {label} map")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("Benchmark output directory must be empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for case in cases:
        case_dir = output_dir / case["id"]
        case_dir.mkdir()
        fixture = manifest.parent / "fixtures" / case["fixture"]
        fixture_digest, base_sha = _fixture_repo(fixture, case_dir / "repo")
        started = time.monotonic()
        try:
            result = run_task(
                task=case["task"],
                repo=case_dir / "repo",
                model=model,
                base_url=base_url,
                worker=worker,
                checks=case.get("checks", []),
                state_dir=case_dir / "state",
                run_id=case["id"],
                mode=case["mode"],
            )
        except Exception as exc:
            result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        latency = time.monotonic() - started
        oracle_result = None
        if case["mode"] == "implementation" and result.get("status") == "ready_for_review":
            try:
                completed = subprocess.run(
                    case["oracle"],
                    cwd=result["worktree"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                oracle_result = {
                    "passed": completed.returncode == 0,
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout[-20_000:],
                    "stderr": completed.stderr[-20_000:],
                }
            except (OSError, subprocess.TimeoutExpired) as exc:
                oracle_result = {"passed": False, "error": str(exc)}
        score = score_case(
            case,
            result,
            oracle_passed=oracle_result["passed"] if oracle_result else None,
            latency_seconds=latency,
            interventions=(interventions or {}).get(case["id"]),
            cost_usd=(costs_usd or {}).get(case["id"]),
        )
        record = {
            "case": case["id"],
            "mode": case["mode"],
            "fixture_sha256": fixture_digest,
            "base_sha": base_sha,
            "result": result,
            "oracle": oracle_result,
            "score": score,
        }
        (case_dir / "result.json").write_text(json.dumps(record, indent=2) + "\n")
        records.append(record)
    summary = {
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "model": model,
        "planner_origin": planner_origin,
        "worker_executable": Path(worker[0]).name if worker else None,
        "cases": [{"id": record["case"], "score": record["score"]} for record in records],
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def _origin(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("Planner URL needs a scheme and host")
    return f"{parsed.scheme}://{parsed.hostname}{':' + str(parsed.port) if parsed.port else ''}"
