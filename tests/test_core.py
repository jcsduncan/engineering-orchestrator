import json
import sqlite3
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

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


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), PlannerHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_local_plan(self):
        plan = plan_task("Do something", "local-test", self.base_url)
        self.assertEqual(plan.steps, ["Inspect"])

    def test_run_records_worktree_and_checks(self):
        with TemporaryDirectory() as root:
            root = Path(root)
            repo = root / "repo"
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
            result = run_task(
                task="Update readme",
                repo=repo,
                model="local-test",
                base_url=self.base_url,
                worker=worker,
                checks=[
                    f'{sys.executable} -c \'from pathlib import Path; assert Path("README.md").read_text() == "after\\n"\''
                ],
                state_dir=root / "state",
            )
            self.assertEqual(result["status"], "ready_for_review")
            self.assertTrue((Path(result["worktree"]) / "README.md").exists())
            self.assertEqual((repo / "README.md").read_text(), "before\n")
            with sqlite3.connect(root / "state/runs.sqlite3") as db:
                self.assertEqual(
                    db.execute("SELECT status FROM runs").fetchone()[0],
                    "ready_for_review",
                )


if __name__ == "__main__":
    unittest.main()
