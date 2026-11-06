from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Plan:
    objective: str
    steps: list[str]
    checks: list[str]
    risks: list[str]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def plan_task(task: str, model: str, base_url: str, timeout: int = 120) -> Plan:
    """Ask a local OpenAI-compatible server for a bounded, structured plan."""
    body = {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an engineering task controller. Return only a JSON object with "
                    "objective (string), steps (array of strings), checks (array of strings), "
                    "and risks (array of strings). Keep each array to at most five items. "
                    "Do not claim to have inspected code. State uncertainty honestly."
                ),
            },
            {"role": "user", "content": task},
        ],
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = json.load(response)
    raw = payload["choices"][0]["message"]["content"].strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    data = json.loads(raw)
    if not isinstance(data.get("objective"), str) or not data["objective"].strip():
        raise ValueError("Planner response has no objective")
    for key in ("steps", "checks", "risks"):
        if not isinstance(data.get(key), list) or any(not isinstance(x, str) for x in data[key]):
            raise ValueError(f"Planner response has invalid {key}")
    return Plan(data["objective"], data["steps"][:5], data["checks"][:5], data["risks"][:5])


def _database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, repo TEXT NOT NULL,
            base_sha TEXT NOT NULL, branch TEXT NOT NULL, worktree TEXT NOT NULL,
            status TEXT NOT NULL, task TEXT NOT NULL, plan_json TEXT NOT NULL,
            result_json TEXT
        )"""
    )
    db.commit()
    return db


def _record(db: sqlite3.Connection, run_id: str, status: str, result: dict | None = None) -> None:
    db.execute(
        "UPDATE runs SET status = ?, result_json = ? WHERE id = ?",
        (status, json.dumps(result) if result is not None else None, run_id),
    )
    db.commit()


def run_task(
    *,
    task: str,
    repo: Path,
    model: str,
    base_url: str,
    worker: list[str],
    checks: list[str],
    state_dir: Path,
    worker_timeout: int = 1800,
    check_timeout: int = 300,
) -> dict:
    if not worker:
        raise ValueError("A coding worker command is required")
    repo = Path(git(repo, "rev-parse", "--show-toplevel"))
    if git(repo, "status", "--porcelain"):
        raise ValueError("Target repository must have a clean worktree")
    base_sha = git(repo, "rev-parse", "HEAD")
    plan = plan_task(task, model, base_url)
    run_id = uuid.uuid4().hex[:12]
    branch = f"agent/{run_id}"
    worktree = state_dir / "worktrees" / run_id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    db = _database(state_dir / "runs.sqlite3")
    try:
        git(repo, "worktree", "add", "-b", branch, str(worktree), base_sha)
        db.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                int(time.time()),
                str(repo),
                base_sha,
                branch,
                str(worktree),
                "running",
                task,
                json.dumps(asdict(plan)),
                None,
            ),
        )
        db.commit()
        prompt = (
            f"Task:\n{task}\n\nPlan from the local controller (verify against code):\n"
            f"{json.dumps(asdict(plan), indent=2)}\n\n"
            "Implement the task in this checkout. Run relevant tests. If the cause or "
            "solution is uncertain, report that clearly; do not invent a fix. "
            "Do not commit, push, or open a pull request."
        )
        # The last argument is the prompt. An adapter can translate this contract for any CLI.
        try:
            completed = subprocess.run(
                [*worker, prompt],
                check=False,
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=worker_timeout,
                env={**os.environ, "ENGINEER_RUN_ID": run_id},
            )
            worker_result = {
                "exit_code": completed.returncode,
                "stdout": completed.stdout[-50000:],
                "stderr": completed.stderr[-50000:],
            }
        except subprocess.TimeoutExpired as exc:
            worker_result = {"exit_code": None, "stdout": "", "stderr": str(exc)}
        check_results = []
        if worker_result["exit_code"] == 0:
            for check in checks:
                argv = shlex.split(check)
                if not argv:
                    raise ValueError("Empty check command")
                try:
                    completed = subprocess.run(
                        argv,
                        check=False,
                        cwd=worktree,
                        capture_output=True,
                        text=True,
                        timeout=check_timeout,
                    )
                    item = {
                        "command": check,
                        "exit_code": completed.returncode,
                        "stdout": completed.stdout[-20000:],
                        "stderr": completed.stderr[-20000:],
                    }
                except subprocess.TimeoutExpired as exc:
                    item = {
                        "command": check,
                        "exit_code": None,
                        "stdout": "",
                        "stderr": str(exc),
                    }
                check_results.append(item)
                if item["exit_code"] != 0:
                    break
        diff = git(worktree, "status", "--short")
        result = {
            "id": run_id,
            "branch": branch,
            "worktree": str(worktree),
            "base_sha": base_sha,
            "plan": asdict(plan),
            "worker": worker_result,
            "checks": check_results,
            "changed_files": diff.splitlines(),
        }
        status = (
            "ready_for_review"
            if (
                worker_result["exit_code"] == 0
                and all(item["exit_code"] == 0 for item in check_results)
                and bool(diff)
            )
            else "needs_review"
        )
        result["status"] = status
        _record(db, run_id, status, result)
        return result
    except Exception as exc:
        _record(db, run_id, "failed", {"error": str(exc)})
        raise
    finally:
        db.close()
