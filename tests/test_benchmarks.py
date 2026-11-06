from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from engineer import bench_cli
from engineer.benchmarks import (
    _fixture_repo,
    load_cases,
    record_measurements,
    run_suite,
    score_case,
)
from engineer.container import ContainerResult, ContainerRuntime


def test_fixture_repository_is_reproducible(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "benchmarks/fixtures/negative_totals"
    first = _fixture_repo(source, tmp_path / "first")
    second = _fixture_repo(source, tmp_path / "second")
    assert first == second
    assert (
        subprocess.check_output(
            ["git", "-C", str(tmp_path / "first"), "status", "--porcelain"], text=True
        )
        == ""
    )


def test_manifest_rejects_duplicate_cases(tmp_path: Path) -> None:
    manifest = tmp_path / "tasks.json"
    case = {
        "id": "duplicate",
        "fixture": "fixture",
        "task": "Fix this",
        "mode": "implementation",
        "oracle": ["python", "-c", "pass"],
    }
    manifest.write_text(json.dumps({"version": 1, "cases": [case, case]}))
    with pytest.raises(ValueError, match="invalid identity"):
        load_cases(manifest)


def test_fixture_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "fixture"
    source.mkdir()
    (source / "outside").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlinks"):
        _fixture_repo(source, tmp_path / "repo")


def test_fixture_git_ignores_inherited_repository_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(__file__).parents[1] / "benchmarks/fixtures/negative_totals"
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing-repo"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "wrong-worktree"))
    _, commit = _fixture_repo(source, tmp_path / "repo")
    assert len(commit) == 40


def test_manifest_case_id_matches_run_id_contract(tmp_path: Path) -> None:
    manifest = tmp_path / "tasks.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "cases": [
                    {
                        "id": "unicode_é",
                        "fixture": "fixture",
                        "task": "Fix this",
                        "mode": "implementation",
                        "oracle": ["python", "-c", "pass"],
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="invalid identity"):
        load_cases(manifest)


def test_score_distinguishes_inconclusive_from_failure() -> None:
    case = {"mode": "investigation", "required_findings": ["cache.py", "cache"]}
    report = {
        "status": "report_for_review",
        "report": {"findings": [{"claim": "cache grows", "evidence": ["cache.py: _CACHE"]}]},
    }
    score = score_case(case, report, oracle_passed=None, latency_seconds=1.2)
    assert score["correctness"] is True
    assert score["validation_quality"] is True
    assert score["cost_usd"] is None
    assert (
        score_case(case, {"status": "inconclusive"}, oracle_passed=None, latency_seconds=1)[
            "outcome"
        ]
        == "inconclusive"
    )
    assert (
        score_case(case, {"status": "failed"}, oracle_passed=None, latency_seconds=1)["outcome"]
        == "failed"
    )


def test_suite_retains_artifacts_when_planner_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ContainerRuntime, "resolve_image", lambda self: self)
    monkeypatch.setattr(
        "engineer.benchmarks.run_task",
        lambda **kwargs: (_ for _ in ()).throw(ConnectionRefusedError("planner unavailable")),
    )
    manifest = Path(__file__).parents[1] / "benchmarks/tasks.json"
    output = tmp_path / "artifacts"
    summary = run_suite(
        manifest=manifest,
        output_dir=output,
        model="unavailable",
        base_url="http://127.0.0.1:1/v1",
        worker=["python", "-c"],
        image="fixture-image",
    )
    assert len(summary["cases"]) == 2
    assert all(item["score"]["outcome"] == "failed" for item in summary["cases"])
    assert (output / "summary.json").exists()
    for case in ("negative_totals", "cache_growth"):
        artifact = json.loads((output / case / "result.json").read_text())
        assert artifact["fixture_sha256"]
        assert artifact["base_sha"]
        assert artifact["result"]["status"] == "failed"
    with pytest.raises(ValueError, match="must be empty"):
        run_suite(
            manifest=manifest,
            output_dir=output,
            model="unavailable",
            base_url="http://127.0.0.1:1/v1",
            worker=["python", "-c"],
            image="fixture-image",
        )


def test_suite_scores_implementation_oracle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = Path(__file__).parents[1] / "benchmarks/tasks.json"
    monkeypatch.setattr(ContainerRuntime, "resolve_image", lambda self: self)

    def fake_container_run(
        self: ContainerRuntime,
        argv: list[str],
        worktree: Path,
        run_id: str,
        timeout: int,
        limit: int,
        *,
        readonly_worktree: bool = False,
    ) -> ContainerResult:
        assert self.image == "fixture-image"
        assert run_id == "negative_totals"
        assert readonly_worktree is True
        completed = subprocess.run(argv, cwd=worktree, capture_output=True, text=True, check=False)
        return ContainerResult(completed.returncode, completed.stdout, completed.stderr)

    monkeypatch.setattr(ContainerRuntime, "run", fake_container_run)

    def fake_run_task(**kwargs: object) -> dict:
        assert kwargs["execution_image"] == "fixture-image"
        repo = kwargs["repo"]
        assert isinstance(repo, Path)
        worktree = repo.parent / "worker-tree"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-qb", "agent/test", str(worktree)],
            check=True,
        )
        if kwargs["mode"] == "implementation":
            (worktree / "ledger.py").write_text(
                "def total(amounts: list[int]) -> int:\n    return sum(amounts)\n"
            )
            return {
                "status": "ready_for_review",
                "worktree": str(worktree),
                "checks": [{"status": "passed"}],
                "validation": {
                    "status": "passed",
                    "diff_sha256_before": "same",
                    "diff_sha256_after": "same",
                },
            }
        return {
            "status": "report_for_review",
            "report": {
                "findings": [
                    {"claim": "cache grows", "evidence": ["cache.py: _CACHE retains keys"]}
                ]
            },
        }

    monkeypatch.setattr("engineer.benchmarks.run_task", fake_run_task)
    summary = run_suite(
        manifest=manifest,
        output_dir=tmp_path / "out",
        model="fixture-model",
        base_url="http://user:secret@127.0.0.1:11434/v1",
        worker=["worker"],
        image="fixture-image",
        interventions={"negative_totals": 0},
        costs_usd={"negative_totals": 0.5},
    )
    assert all(case["score"]["correctness"] for case in summary["cases"])
    assert summary["planner_origin"] == "http://127.0.0.1:11434"
    assert summary["cases"][0]["image_id"] == "fixture-image"
    assert summary["cases"][0]["score"]["cost_usd"] == 0.5
    assert json.loads((tmp_path / "out/negative_totals/result.json").read_text())["oracle"][
        "passed"
    ]


def test_suite_rejects_invalid_intervention_map(tmp_path: Path) -> None:
    manifest = Path(__file__).parents[1] / "benchmarks/tasks.json"
    with pytest.raises(ValueError, match="Invalid interventions"):
        run_suite(
            manifest=manifest,
            output_dir=tmp_path / "out",
            model="model",
            base_url="http://localhost:11434/v1",
            worker=["worker"],
            image="fixture-image",
            interventions=[],  # type: ignore[arg-type]
        )
    assert not (tmp_path / "out").exists()


def test_record_measurements_preserves_raw_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "run"
    output.mkdir()
    original = {
        "manifest_sha256": "fixture",
        "cases": [
            {
                "id": "negative_totals",
                "score": {"correctness": True, "interventions": None, "cost_usd": None},
            }
        ],
    }
    (output / "summary.json").write_text(json.dumps(original))
    measured = record_measurements(
        output, interventions={"negative_totals": 2}, costs_usd={"negative_totals": 0.5}
    )
    assert measured["cases"][0]["score"]["interventions"] == 2
    assert measured["cases"][0]["score"]["cost_usd"] == 0.5
    assert json.loads((output / "summary.json").read_text()) == original
    assert json.loads((output / "measured_summary.json").read_text()) == measured
    with pytest.raises(ValueError, match="Invalid costs_usd"):
        record_measurements(output, costs_usd={"unknown": 1.0})


def test_benchmark_cli_accepts_fire_parsed_measurement_maps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bench_cli._mapping("") is None
    calls = []

    def fake_run_suite(**kwargs):
        calls.append(kwargs)
        return {"cases": []}

    def fake_measure(output_dir, **kwargs):
        calls.append({"output_dir": output_dir, **kwargs})
        return {"cases": []}

    monkeypatch.setattr(bench_cli, "run_suite", fake_run_suite)
    monkeypatch.setattr(bench_cli, "record_measurements", fake_measure)
    bench_cli.run(
        "tasks.json",
        str(tmp_path / "out"),
        "worker -p",
        "image",
        model="local",
        interventions={"negative_totals": 0},
        costs_usd={"negative_totals": 0.5},
    )
    bench_cli.measure(
        str(tmp_path / "out"),
        interventions={"negative_totals": 1},
        costs_usd={"negative_totals": 0.6},
    )
    assert calls[0]["interventions"] == {"negative_totals": 0}
    assert calls[0]["costs_usd"] == {"negative_totals": 0.5}
    assert calls[1]["interventions"] == {"negative_totals": 1}
    assert calls[1]["costs_usd"] == {"negative_totals": 0.6}
    assert capsys.readouterr().out.count('"cases": []') == 2
