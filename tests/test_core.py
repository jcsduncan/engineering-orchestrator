import json
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

import pytest

from engineer.core import plan_task, run_task


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
        ('{"objective": "", "steps": [], "checks": [], "risks": []}', "no objective"),
        ('{"objective": "x", "steps": "not a list", "checks": [], "risks": []}', "steps"),
        ('{"objective": "x", "steps": [], "checks": [1], "risks": []}', "checks"),
        ('{"objective": "x", "steps": [], "checks": [], "risks": null}', "risks"),
    ],
)
def test_plan_rejects_invalid_response(monkeypatch, content, message):
    response = {"choices": [{"message": {"content": content}}]}
    monkeypatch.setattr(
        "engineer.core.urllib.request.urlopen",
        lambda _request, timeout: BytesIO(json.dumps(response).encode()),
    )
    with pytest.raises(ValueError, match=message):
        plan_task("Do something", "local-test", "http://localhost:11434/v1")


def test_run_records_worktree_and_checks(planner_url, tmp_path):
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
        state_dir=tmp_path / "state",
    )
    assert result["status"] == "ready_for_review"
    assert (Path(result["worktree"]) / "README.md").exists()
    assert (repo / "README.md").read_text() == "before\n"
    with sqlite3.connect(tmp_path / "state/runs.sqlite3") as db:
        assert db.execute("SELECT status FROM runs").fetchone()[0] == "ready_for_review"
