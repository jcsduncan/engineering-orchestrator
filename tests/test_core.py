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
    assert (Path(result["worktree"]) / "README.md").exists()
    assert (repo / "README.md").read_text() == "before\n"
    with sqlite3.connect(tmp_path / "state/runs.sqlite3") as db:
        assert db.execute("SELECT status FROM runs").fetchone()[0] == "ready_for_review"
        providers = json.loads(db.execute("SELECT providers_json FROM runs").fetchone()[0])
    assert providers == result["providers"]
    assert providers["planner"]["configuration"]["model"] == "local-test"
    assert providers["worker"]["configuration"]["executable"] == Path(sys.executable).name


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
