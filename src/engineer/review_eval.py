"""Measure the effect of advisory review on saved benchmark candidates."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path

from .review import ReviewProvider, review_run

HOLD_SEVERITIES = {"critical", "major"}


def score_review_impact(cases: list[dict]) -> dict:
    """Compare unreviewed acceptance with a hypothetical high-severity hold."""
    eligible = [case for case in cases if case["oracle_passed"] is not None]
    bad = [case for case in eligible if case["oracle_passed"] is False]
    good = [case for case in eligible if case["oracle_passed"] is True]
    return {
        "eligible": len(eligible),
        "completed_reviews": sum(case["review_status"] == "completed" for case in eligible),
        "baseline_bad_accepts": len(bad),
        "reviewed_bad_accepts": sum(not case["hold"] for case in bad),
        "bad_candidates_flagged": sum(case["hold"] for case in bad),
        "good_candidates_flagged": sum(case["hold"] for case in good),
    }


def review_benchmark_output(output_dir: Path, reviewer: ReviewProvider) -> dict:
    """Review completed implementation candidates without changing raw benchmark results."""
    output_dir = output_dir.expanduser().resolve()
    baseline = json.loads((output_dir / "summary.json").read_text())
    cases = []
    for summary_case in baseline["cases"]:
        case_id = summary_case["id"]
        if not isinstance(case_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", case_id
        ):
            raise ValueError("Invalid benchmark case ID")
        case_dir = output_dir / case_id
        record = json.loads((case_dir / "result.json").read_text())
        if record["case"] != case_id:
            raise ValueError("Benchmark case identity does not match its artifact")
        if record["mode"] != "implementation" or record["result"]["status"] != "ready_for_review":
            continue
        try:
            review = review_run(case_dir / "state", case_id, reviewer)
        except ValueError as exc:
            review = {
                "run_id": case_id,
                "diff_sha256": (record["result"].get("validation") or {}).get("diff_sha256_after"),
                "provider": asdict(reviewer.info),
                "status": "error",
                "findings": [],
                "error": str(exc)[:500],
            }
        (case_dir / "review.json").write_text(json.dumps(review, indent=2) + "\n")
        oracle = record["oracle"]
        oracle_passed = oracle["passed"] if oracle and type(oracle.get("passed")) is bool else None
        hold = review["status"] == "completed" and any(
            finding["severity"] in HOLD_SEVERITIES for finding in review["findings"]
        )
        cases.append(
            {
                "id": case_id,
                "oracle_passed": oracle_passed,
                "review_status": review["status"],
                "findings": len(review["findings"]),
                "hold": hold,
            }
        )
    report = {
        "manifest_sha256": baseline["manifest_sha256"],
        "reviewer": asdict(reviewer.info),
        "cases": cases,
        "impact_proxy": score_review_impact(cases),
    }
    (output_dir / "review_summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return report
