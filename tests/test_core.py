import json
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

import pytest

from engineer.core import execute_task, plan_task, run_task
from engineer.providers import (
    CommandWorker,
    OpenAICompatiblePlanner,
    Plan,
    ProviderInfo,
    WorkerResult,
)
from engineer.reports import REPORT_PREFIX


class PlannerHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        assert self.path == "/v1/chat/completions"
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert request["model"] == "local-test"
        content = json.dumps(
            {
                "objective": "Make a change",
                "steps": ["Inspect"],
                "checks": ["Run tests"],
                "risks": [],
            }
        )
        response = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_args):
        pass


@pytest.fixture
def planner_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), PlannerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_local_plan(planner_url):
    plan = plan_task("Do something", "local-test", planner_url)
    assert plan.steps == ["Inspect"]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            '{"objective": "", "steps": [], "checks": [], "risks": []}',
            "^Planner response has no objective$",
        ),
        (
            '{"objective": "x", "steps": "not a list", "checks": [], "risks": []}',
            "^Planner response has invalid steps$",
        ),
        (
            '{"objective": "x", "steps": [], "checks": [1], "risks": []}',
            "^Planner response has invalid checks$",
        ),
        (
            '{"objective": "x", "steps": [], "checks": [], "risks": null}',
            "^Planner response has invalid risks$",
        ),
    ],
)
def test_plan_rejects_invalid_response(monkeypatch, content, message):
    response = {"choices": [{"message": {"content": content}}]}
    monkeypatch.setattr(
        "engineer.providers.urllib.request.urlopen",
        lambda _request, timeout: BytesIO(json.dumps(response).encode()),
    )
    with pytest.raises(ValueError, match=message):
        plan_task("Do something", "local-test", "http://localhost:11434/v1")


def test_plan_rejects_malformed_chat_completion(monkeypatch):
    monkeypatch.setattr(
        "engineer.providers.urllib.request.urlopen",
        lambda _request, timeout: BytesIO(b'{"choices": []}'),
    )
    with pytest.raises(ValueError, match="Planner response is not valid chat-completion JSON"):
        plan_task("Do something", "local-test", "http://localhost:11434/v1")


def test_plan_rejects_invalid_json(monkeypatch):
    monkeypatch.setattr(
        "engineer.providers.urllib.request.urlopen",
        lambda _request, timeout: BytesIO(b"not json"),
    )
    with pytest.raises(ValueError, match="Planner response is not valid chat-completion JSON"):
        plan_task("Do something", "local-test", "http://localhost:11434/v1")


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("before\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    return repo


def test_run_records_worktree_and_checks(planner_url, tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    with sqlite3.connect(state_dir / "runs.sqlite3") as db:
        db.execute(
            """CREATE TABLE runs (
                id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, repo TEXT NOT NULL,
                base_sha TEXT NOT NULL, branch TEXT NOT NULL, worktree TEXT NOT NULL,
                status TEXT NOT NULL, task TEXT NOT NULL, plan_json TEXT NOT NULL,
                result_json TEXT
            )"""
        )
    worker = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('README.md').write_text('after\\n')",
        "ignored",
    ]
    # run_task appends the task prompt after the worker prefix.
    check_script = 'from pathlib import Path; assert Path("README.md").read_text() == "after\\n"'
    result = run_task(
        task="Update readme",
        repo=repo,
        model="local-test",
        base_url=planner_url,
        worker=worker,
        checks=[f"{sys.executable} -c '{check_script}'"],
        state_dir=state_dir,
    )
    assert result["status"] == "ready_for_review"
    assert result["checks"][0]["status"] == "passed"
    assert result["checks"][0]["argv"][:2] == [sys.executable, "-c"]
    assert result["validation"]["status"] == "passed"
    assert result["validation"]["diff_sha256_before"] == result["validation"]["diff_sha256_after"]
    assert (Path(result["worktree"]) / "README.md").exists()
    assert (repo / "README.md").read_text() == "before\n"
    with sqlite3.connect(tmp_path / "state/runs.sqlite3") as db:
        assert db.execute("SELECT status FROM runs").fetchone()[0] == "ready_for_review"
        assert db.execute("SELECT mode FROM runs").fetchone()[0] == "implementation"
        providers = json.loads(db.execute("SELECT providers_json FROM runs").fetchone()[0])
        stored = json.loads(db.execute("SELECT result_json FROM runs").fetchone()[0])
    assert providers == result["providers"]
    assert providers["planner"]["configuration"]["model"] == "local-test"
    assert providers["worker"]["configuration"]["executable"] == Path(sys.executable).name
    assert stored["validation"] == result["validation"]

    no_checks = run_task(
        task="Update readme",
        repo=repo,
        model="local-test",
        base_url=planner_url,
        worker=worker,
        checks=[],
        state_dir=tmp_path / "state-without-checks",
    )
    assert no_checks["status"] == "needs_review"
    assert no_checks["validation"]["status"] == "not_run"


def test_injected_providers_execute_independently(tmp_path):
    repo = make_repo(tmp_path)

    class Planner:
        info = ProviderInfo("test-planner", {"variant": "fixed"})

        def plan(self, task):
            assert task == "Update readme"
            return Plan("Update readme", ["Edit"], ["Check"], [])

    class Worker:
        info = ProviderInfo("test-worker", {"variant": "fixed"})

        def execute(self, prompt, worktree, run_id):
            assert "Update readme" in prompt
            assert run_id
            (worktree / "README.md").write_text("after\n")
            return WorkerResult(0, "edited", "")

    result = execute_task(
        task="Update readme",
        repo=repo,
        planner=Planner(),
        worker=Worker(),
        checks=[f"{sys.executable} -c 'print(1)'"],
        state_dir=tmp_path / "state",
    )
    assert result["status"] == "ready_for_review"
    assert result["providers"]["planner"]["name"] == "test-planner"
    assert result["providers"]["worker"]["name"] == "test-worker"


def test_unsupported_untracked_entry_preserves_worker_result(tmp_path):
    class Planner:
        info = ProviderInfo("test", {})

        def plan(self, _task):
            return Plan("Task", [], [], [])

    class Worker:
        info = ProviderInfo("test", {})

        def execute(self, _prompt, worktree, _run_id):
            subprocess.run(["git", "init", "-q", str(worktree / "nested")], check=True)
            return WorkerResult(0, "nested repository created", "")

    result = execute_task(
        task="task",
        repo=make_repo(tmp_path),
        planner=Planner(),
        worker=Worker(),
        checks=[f"{sys.executable} -c 'print(1)'"],
        state_dir=tmp_path / "state",
    )

    assert result["status"] == "needs_review"
    assert result["worker"]["stdout"] == "nested repository created"
    assert result["validation"]["status"] == "invalidated"
    assert result["checks"] == []
    assert result["changed_files"]


@pytest.mark.parametrize("tamper_in_check", [False, True])
def test_changed_git_pointer_is_rejected_before_host_git(tmp_path, tamper_in_check):
    class Planner:
        info = ProviderInfo("test-planner", {})

        def plan(self, _task):
            return Plan("Task", [], [], [])

    class Worker:
        info = ProviderInfo("test-worker", {})

        def execute(self, _prompt, worktree, _run_id):
            if not tamper_in_check:
                (worktree / ".git").write_text("gitdir: /etc\n")
            return WorkerResult(0, "", "")

    class CheckRunner:
        def execute(self, _command, worktree, _run_id, _timeout):
            (worktree / ".git").write_text("gitdir: /etc\n")
            return WorkerResult(0, "", "")

    state_dir = tmp_path / "state"
    with pytest.raises(ValueError, match="Worker changed Git worktree metadata pointer"):
        execute_task(
            task="Task",
            repo=make_repo(tmp_path),
            planner=Planner(),
            worker=Worker(),
            checks=["echo ok"],
            check_runner=CheckRunner(),
            state_dir=state_dir,
        )
    with sqlite3.connect(state_dir / "runs.sqlite3") as db:
        assert db.execute("SELECT status FROM runs").fetchone()[0] == "failed"


def test_invalid_planning_provider_output_is_rejected(tmp_path):
    class Planner:
        info = ProviderInfo("broken", {})

        def plan(self, _task):
            return Plan("", [], [], [])

    class Worker:
        info = ProviderInfo("unused", {})

        def execute(self, _prompt, _worktree, _run_id):
            pytest.fail("Worker must not run")

    with pytest.raises(ValueError, match="Planning provider returned an invalid Plan"):
        execute_task(
            task="task",
            repo=make_repo(tmp_path),
            planner=Planner(),
            worker=Worker(),
            checks=[],
            state_dir=tmp_path / "state",
        )


@pytest.mark.parametrize(
    ("info", "message"),
    [
        ({"name": "broken"}, "Provider returned invalid ProviderInfo"),
        (ProviderInfo("broken", {"value": object()}), "Provider metadata is not JSON-serializable"),
    ],
)
def test_invalid_provider_metadata_creates_no_worktree(tmp_path, info, message):
    class Planner:
        def plan(self, _task):
            return Plan("Task", [], [], [])

    class Worker:
        def execute(self, _prompt, _worktree, _run_id):
            pytest.fail("Worker must not run")

    planner = Planner()
    planner.info = info
    worker = Worker()
    worker.info = ProviderInfo("test", {})
    state_dir = tmp_path / "state"
    with pytest.raises(ValueError, match=message):
        execute_task(
            task="task",
            repo=make_repo(tmp_path),
            planner=planner,
            worker=worker,
            checks=[],
            state_dir=state_dir,
        )
    assert not state_dir.exists()


def test_invalid_worker_provider_output_fails_run(tmp_path):
    class Planner:
        info = ProviderInfo("test", {})

        def plan(self, _task):
            return Plan("Task", [], [], [])

    class Worker:
        info = ProviderInfo("broken", {})

        def execute(self, _prompt, _worktree, _run_id):
            return {"exit_code": 0}

    state_dir = tmp_path / "state"
    with pytest.raises(ValueError, match="Worker provider returned an invalid WorkerResult"):
        execute_task(
            task="task",
            repo=make_repo(tmp_path),
            planner=Planner(),
            worker=Worker(),
            checks=[],
            state_dir=state_dir,
        )
    with sqlite3.connect(state_dir / "runs.sqlite3") as db:
        assert db.execute("SELECT status FROM runs").fetchone()[0] == "failed"


def test_provider_metadata_omits_credentials():
    planner = OpenAICompatiblePlanner(
        "local-test", "http://user:secret@localhost:11434/v1?key=secret"
    )
    worker = CommandWorker(("qwen", "--token=secret"))
    metadata = json.dumps({"planner": planner.info.__dict__, "worker": worker.info.__dict__})
    assert "secret" not in metadata
    assert planner.info.configuration["endpoint"] == "http://localhost:11434"


def investigation_worker(report, exit_code=0, change=False):
    class Worker:
        info = ProviderInfo("test", {})

        def execute(self, prompt, worktree, _run_id):
            assert "Investigate this task without changing files" in prompt
            if change:
                (worktree / "README.md").write_text("changed\n")
            output = "diagnostic output\n" + REPORT_PREFIX + json.dumps(report)
            return WorkerResult(exit_code, output, "")

    return Worker()


class InvestigationPlanner:
    info = ProviderInfo("test", {})

    def plan(self, _task):
        return Plan("Investigate", ["Inspect"], ["Check"], [])


def report(outcome="substantiated"):
    return {
        "outcome": outcome,
        "summary": (
            "The evidence supports this finding" if outcome == "substantiated" else "Unknown"
        ),
        "findings": [{"claim": "A condition exists", "evidence": ["README.md:1"]}]
        if outcome == "substantiated"
        else [],
        "commands": [{"argv": ["git", "status", "--short"], "result": "clean"}],
        "uncertainty": [] if outcome == "substantiated" else ["Need production logs"],
    }


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("substantiated", "report_for_review"), ("inconclusive", "inconclusive")],
)
def test_investigation_records_report_without_diff(tmp_path, outcome, expected):
    state_dir = tmp_path / "state"
    result = execute_task(
        task="Investigate",
        repo=make_repo(tmp_path),
        planner=InvestigationPlanner(),
        worker=investigation_worker(report(outcome)),
        checks=[f"{sys.executable} -c 'print(1)'"],
        state_dir=state_dir,
        mode="investigation",
    )
    assert result["status"] == expected
    assert result["mode"] == "investigation"
    assert result["report"] == report(outcome)
    assert result["changed_files"] == []
    assert result["validation"]["status"] == "passed"
    with sqlite3.connect(state_dir / "runs.sqlite3") as db:
        stored_mode, stored_status, stored_json = db.execute(
            "SELECT mode, status, result_json FROM runs"
        ).fetchone()
    assert (stored_mode, stored_status) == ("investigation", expected)
    assert json.loads(stored_json)["report"] == result["report"]


def test_investigation_replay_preserves_mode_and_rejects_mode_change(tmp_path):
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    worker = investigation_worker(report())
    kwargs = {
        "task": "Investigate",
        "repo": repo,
        "planner": InvestigationPlanner(),
        "worker": worker,
        "checks": [],
        "state_dir": state_dir,
        "run_id": "investigation-1",
    }
    first = execute_task(**kwargs, mode="investigation")
    assert execute_task(**kwargs, mode="investigation") == first
    with pytest.raises(ValueError, match="Run ID belongs to a different task or configuration"):
        execute_task(**kwargs, mode="implementation")


def test_implementation_request_keeps_legacy_digest_shape(tmp_path):
    class Worker:
        info = ProviderInfo("test", {})

        def execute(self, _prompt, _worktree, _run_id):
            return WorkerResult(0, "No change", "")

    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    kwargs = {
        "task": "Inspect",
        "repo": repo,
        "planner": InvestigationPlanner(),
        "worker": Worker(),
        "checks": [],
        "state_dir": state_dir,
        "run_id": "legacy-implementation",
    }
    first = execute_task(**kwargs)
    with sqlite3.connect(state_dir / "runs.sqlite3") as db:
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM runs").fetchone()[0])
    assert "mode" not in snapshot
    assert execute_task(**kwargs) == first


@pytest.mark.parametrize(
    ("worker_report", "exit_code", "change", "reason"),
    [
        ({"outcome": "substantiated"}, 0, False, "needs a summary"),
        (report(), 1, False, "Worker did not exit successfully"),
        (report(), 0, True, "Investigation changed the worktree"),
        ({**report(), "findings": []}, 0, False, "needs an evidenced finding"),
        ({**report("inconclusive"), "uncertainty": []}, 0, False, "needs an uncertainty"),
    ],
)
def test_investigation_failure_keeps_evidence(tmp_path, worker_report, exit_code, change, reason):
    result = execute_task(
        task="Investigate",
        repo=make_repo(tmp_path),
        planner=InvestigationPlanner(),
        worker=investigation_worker(worker_report, exit_code, change),
        checks=[],
        state_dir=tmp_path / "state",
        mode="investigation",
    )
    assert result["status"] == "failed"
    assert reason in result["failure_reason"]
    assert result["worker"]["stdout"].startswith("diagnostic output")
    assert result["validation"]["status"] == "not_run"


def test_investigation_keeps_first_failure_reason(tmp_path):
    result = execute_task(
        task="Investigate",
        repo=make_repo(tmp_path),
        planner=InvestigationPlanner(),
        worker=investigation_worker(report(), exit_code=1, change=True),
        checks=[],
        state_dir=tmp_path / "state",
        mode="investigation",
    )
    assert result["failure_reason"] == "Worker did not exit successfully"
    assert result["changed_files"] == ["M README.md"]


def test_investigation_failed_check_keeps_report_and_check_evidence(tmp_path):
    result = execute_task(
        task="Investigate",
        repo=make_repo(tmp_path),
        planner=InvestigationPlanner(),
        worker=investigation_worker(report()),
        checks=[f"{sys.executable} -c 'raise SystemExit(2)'"],
        state_dir=tmp_path / "state",
        mode="investigation",
    )
    assert result["status"] == "failed"
    assert result["report"] == report()
    assert result["checks"][0]["exit_code"] == 2
    assert result["validation"]["status"] == "failed"
    assert result["failure_reason"] == "Validation failed: check failed"


def test_investigation_missing_report_is_failed(tmp_path):
    class Worker:
        info = ProviderInfo("test", {})

        def execute(self, _prompt, _worktree, _run_id):
            return WorkerResult(0, "No report", "")

    result = execute_task(
        task="Investigate",
        repo=make_repo(tmp_path),
        planner=InvestigationPlanner(),
        worker=Worker(),
        checks=[],
        state_dir=tmp_path / "state",
        mode="investigation",
    )
    assert result["status"] == "failed"
    assert result["failure_reason"] == "Worker did not provide an investigation report"


def test_investigation_malformed_json_is_failed(tmp_path):
    class Worker:
        info = ProviderInfo("test", {})

        def execute(self, _prompt, _worktree, _run_id):
            return WorkerResult(0, REPORT_PREFIX + "{invalid", "")

    result = execute_task(
        task="Investigate",
        repo=make_repo(tmp_path),
        planner=InvestigationPlanner(),
        worker=Worker(),
        checks=[],
        state_dir=tmp_path / "state",
        mode="investigation",
    )
    assert result["status"] == "failed"
    assert result["failure_reason"] == "Investigation report is not valid JSON"


def test_empty_implementation_run_is_not_reviewable(tmp_path):
    class Worker:
        info = ProviderInfo("test", {})

        def execute(self, _prompt, _worktree, _run_id):
            return WorkerResult(0, "Done", "")

    result = execute_task(
        task="Do something",
        repo=make_repo(tmp_path),
        planner=InvestigationPlanner(),
        worker=Worker(),
        checks=[f"{sys.executable} -c 'print(1)'"],
        state_dir=tmp_path / "state",
    )
    assert result["status"] == "needs_review"
    assert result["changed_files"] == []


def test_provider_timeout_errors(monkeypatch, tmp_path):
    def timeout(*_args, **_kwargs):
        raise TimeoutError("socket timed out")

    monkeypatch.setattr("engineer.providers.urllib.request.urlopen", timeout)
    with pytest.raises(TimeoutError, match="Planner timed out after 3 seconds"):
        OpenAICompatiblePlanner("local-test", "http://localhost:11434/v1", 3).plan("task")

    def worker_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["qwen"], 2)

    monkeypatch.setattr("engineer.providers.subprocess.run", worker_timeout)
    outcome = CommandWorker(("qwen",), 2).execute("task", tmp_path, "run-1")
    assert outcome == WorkerResult(None, "", "Worker timed out after 2 seconds")
