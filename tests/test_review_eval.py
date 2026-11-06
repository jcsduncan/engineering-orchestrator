import json

import pytest

from engineer.providers import ProviderInfo
from engineer.review_eval import review_benchmark_output


class Reviewer:
    info = ProviderInfo("fixture-reviewer", {"variant": "fixed"})


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
