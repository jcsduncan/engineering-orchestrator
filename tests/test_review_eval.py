import json
import subprocess
from pathlib import Path

import pytest

from engineer.benchmarks import run_suite
from engineer.container import ContainerResult, ContainerRuntime
from engineer.core import execute_task
from engineer.providers import Plan, ProviderInfo, WorkerResult
from engineer.review_eval import review_benchmark_output


class Reviewer:
    info = ProviderInfo("fixture-reviewer", {"variant": "fixed"})

    def review(self, _context):
        return {"findings": []}


def test_review_impact_compares_bad_acceptance_and_good_false_flags(tmp_path, monkeypatch):
    summary = {
        "manifest_sha256": "a" * 64,
        "cases": [{"id": "bad"}, {"id": "good"}, {"id": "investigation"}],
    }
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    for case_id, mode, passed in (
        ("bad", "implementation", False),
        ("good", "implementation", True),
        ("investigation", "investigation", None),
    ):
        case_dir = tmp_path / case_id
        case_dir.mkdir()
        record = {
            "case": case_id,
            "mode": mode,
            "result": {
                "status": "ready_for_review" if mode == "implementation" else "inconclusive"
            },
            "oracle": {"passed": passed} if passed is not None else None,
        }
        (case_dir / "result.json").write_text(json.dumps(record))
    calls = []

    def fake_review(state_dir, run_id, reviewer):
        calls.append((state_dir, run_id, reviewer))
        findings = (
            [
                {
                    "severity": "major",
                    "file": "README.md",
                    "side": "new",
                    "line": 1,
                    "issue": "bug",
                    "evidence": "x",
                }
            ]
            if run_id == "bad"
            else []
        )
        return {"status": "completed", "findings": findings}

    monkeypatch.setattr("engineer.review_eval.review_run", fake_review)
    result = review_benchmark_output(tmp_path, Reviewer())
    assert result["impact_proxy"] == {
        "eligible": 2,
        "completed_reviews": 2,
        "baseline_bad_accepts": 1,
        "reviewed_bad_accepts": 0,
        "bad_candidates_flagged": 1,
        "good_candidates_flagged": 0,
    }
    assert [item[1] for item in calls] == ["bad", "good"]
    assert json.loads((tmp_path / "review_summary.json").read_text()) == result
    assert not (tmp_path / "investigation/review.json").exists()
    assert json.loads((tmp_path / "bad/result.json").read_text())["oracle"] == {"passed": False}


def test_review_impact_rejects_unsafe_case_id(tmp_path):
    (tmp_path / "summary.json").write_text(
        json.dumps({"manifest_sha256": "a" * 64, "cases": [{"id": "../outside"}]})
    )
    with pytest.raises(ValueError, match="Invalid benchmark case ID"):
        review_benchmark_output(tmp_path, Reviewer())


def test_review_benchmark_consumes_real_run_artifact(tmp_path, monkeypatch):
    class Planner:
        info = ProviderInfo("fixture-planner", {})

        def plan(self, _task):
            return Plan("Fix totals", ["Edit"], ["Check"], [])

    class Worker:
        info = ProviderInfo("fixture-worker", {})

        def execute(self, _prompt, worktree, _run_id):
            (worktree / "ledger.py").write_text(
                "def total(amounts: list[int]) -> int:\n    return sum(amounts)\n"
            )
            return WorkerResult(0, "fixed", "")

    def fake_run_task(**kwargs):
        if kwargs["mode"] == "investigation":
            return {"status": "inconclusive"}
        return execute_task(
            task=kwargs["task"],
            repo=kwargs["repo"],
            planner=Planner(),
            worker=Worker(),
            checks=kwargs["checks"],
            state_dir=kwargs["state_dir"],
            run_id=kwargs["run_id"],
        )

    def fake_container_run(self, argv, worktree, _run_id, _timeout, _limit, *, readonly_worktree):
        assert readonly_worktree is True
        result = subprocess.run(argv, cwd=worktree, capture_output=True, text=True, check=False)
        return ContainerResult(result.returncode, result.stdout, result.stderr)

    monkeypatch.setattr("engineer.benchmarks.run_task", fake_run_task)
    monkeypatch.setattr(ContainerRuntime, "resolve_image", lambda self: self)
    monkeypatch.setattr(ContainerRuntime, "run", fake_container_run)
    output = tmp_path / "artifacts"
    manifest = Path(__file__).parents[1] / "benchmarks/tasks.json"
    run_suite(
        manifest=manifest,
        output_dir=output,
        model="fixture-model",
        base_url="http://127.0.0.1:11434/v1",
        worker=["worker"],
        image="fixture-image",
    )
    report = review_benchmark_output(output, Reviewer())
    assert report["impact_proxy"]["eligible"] == 1
    assert report["impact_proxy"]["completed_reviews"] == 1
    assert report["cases"][0]["oracle_passed"] is True
    assert json.loads((output / "negative_totals/review.json").read_text())["status"] == "completed"
