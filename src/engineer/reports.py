from __future__ import annotations

import json

REPORT_PREFIX = "ENGINEERING_REPORT_JSON="


def parse_investigation_report(stdout: str) -> dict:
    """Parse the worker's final report from its bounded output."""
    lines = stdout.rstrip().splitlines()
    if not lines or not lines[-1].startswith(REPORT_PREFIX):
        raise ValueError("Worker did not provide an investigation report")
    try:
        report = json.loads(lines[-1].removeprefix(REPORT_PREFIX))
    except json.JSONDecodeError as exc:
        raise ValueError("Investigation report is not valid JSON") from exc
    if not isinstance(report, dict) or report.get("outcome") not in {
        "substantiated",
        "inconclusive",
    }:
        raise ValueError("Investigation report has an invalid outcome")
    if not isinstance(report.get("summary"), str) or not report["summary"].strip():
        raise ValueError("Investigation report needs a summary")
    findings = report.get("findings")
    if not isinstance(findings, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("claim"), str)
        or not item["claim"].strip()
        or not isinstance(item.get("evidence"), list)
        or not item["evidence"]
        or any(not isinstance(value, str) or not value.strip() for value in item["evidence"])
        for item in findings
    ):
        raise ValueError("Investigation report has invalid findings or evidence")
    commands = report.get("commands")
    if not isinstance(commands, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("argv"), list)
        or not item["argv"]
        or any(not isinstance(value, str) or not value for value in item["argv"])
        or not isinstance(item.get("result"), str)
        or not item["result"].strip()
        for item in commands
    ):
        raise ValueError("Investigation report has invalid commands")
    uncertainty = report.get("uncertainty")
    if not isinstance(uncertainty, list) or any(
        not isinstance(value, str) or not value.strip() for value in uncertainty
    ):
        raise ValueError("Investigation report has invalid uncertainty")
    if report["outcome"] == "substantiated" and not findings:
        raise ValueError("Substantiated report needs an evidenced finding")
    if report["outcome"] == "inconclusive" and not uncertainty:
        raise ValueError("Inconclusive report needs an uncertainty")
    return {
        "outcome": report["outcome"],
        "summary": report["summary"],
        "findings": findings,
        "commands": commands,
        "uncertainty": uncertainty,
    }
