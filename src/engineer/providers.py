from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .cancellation import run_cancellable_command
from .capabilities import command_digest


@dataclass(frozen=True)
class Plan:
    objective: str
    steps: list[str]
    checks: list[str]
    risks: list[str]


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    configuration: dict[str, str | int]


@dataclass(frozen=True)
class WorkerResult:
    exit_code: int | None
    stdout: str
    stderr: str


class PlanningProvider(Protocol):
    @property
    def info(self) -> ProviderInfo: ...

    def plan(self, task: str) -> Plan: ...


class WorkerProvider(Protocol):
    @property
    def info(self) -> ProviderInfo: ...

    def execute(self, prompt: str, worktree: Path, run_id: str) -> WorkerResult: ...


class CheckRunner(Protocol):
    def execute(
        self, command: list[str], worktree: Path, run_id: str, timeout: int
    ) -> WorkerResult: ...


def plan_task(task: str, model: str, base_url: str, timeout: int = 120) -> Plan:
    """Ask an OpenAI-compatible server for a bounded, structured plan."""
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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.load(response)
    except TimeoutError as exc:
        raise TimeoutError(f"Planner timed out after {timeout} seconds") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("Planner response is not valid chat-completion JSON") from exc
    try:
        raw = payload["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
    except (KeyError, IndexError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("Planner response is not valid chat-completion JSON") from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("objective"), str)
        or not data["objective"].strip()
    ):
        raise ValueError("Planner response has no objective")
    for key in ("steps", "checks", "risks"):
        if not isinstance(data.get(key), list) or any(not isinstance(x, str) for x in data[key]):
            raise ValueError(f"Planner response has invalid {key}")
    return Plan(data["objective"], data["steps"][:5], data["checks"][:5], data["risks"][:5])


@dataclass(frozen=True)
class OpenAICompatiblePlanner:
    model: str
    base_url: str
    timeout: int = 120

    def __post_init__(self) -> None:
        endpoint = urlsplit(self.base_url)
        if (
            not self.model.strip()
            or endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
        ):
            raise ValueError("Planner requires a model and an HTTP endpoint")
        if type(self.timeout) is not int or self.timeout <= 0:
            raise ValueError("Planner timeout must be positive")

    @property
    def info(self) -> ProviderInfo:
        endpoint = urlsplit(self.base_url)
        host = endpoint.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if endpoint.port is not None:
            host = f"{host}:{endpoint.port}"
        safe_url = f"{endpoint.scheme}://{host}"
        return ProviderInfo(
            "openai-compatible",
            {"model": self.model, "endpoint": safe_url, "timeout_seconds": self.timeout},
        )

    def plan(self, task: str) -> Plan:
        return plan_task(task, self.model, self.base_url, self.timeout)


@dataclass(frozen=True)
class CommandWorker:
    command: tuple[str, ...]
    timeout: int = 1800
    cancel_reason: Callable[[str], str | None] | None = None

    def __post_init__(self) -> None:
        if not self.command or any(not arg for arg in self.command):
            raise ValueError("A coding worker command is required")
        if type(self.timeout) is not int or self.timeout <= 0:
            raise ValueError("Worker timeout must be positive")

    @property
    def info(self) -> ProviderInfo:
        command_hash = command_digest(self.command)
        return ProviderInfo(
            "command",
            {
                "executable": Path(self.command[0]).name,
                "command_sha256": command_hash,
                "timeout_seconds": self.timeout,
            },
        )

    def execute(self, prompt: str, worktree: Path, run_id: str) -> WorkerResult:
        if self.cancel_reason is not None:
            exit_code, stdout, stderr = run_cancellable_command(
                [*self.command, prompt],
                worktree,
                run_id,
                self.timeout,
                50000,
                self.cancel_reason,
                env={**os.environ, "ENGINEER_RUN_ID": run_id},
            )
            return WorkerResult(exit_code, stdout, stderr)
        try:
            completed = subprocess.run(
                [*self.command, prompt],
                check=False,
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env={**os.environ, "ENGINEER_RUN_ID": run_id},
            )
        except subprocess.TimeoutExpired:
            return WorkerResult(None, "", f"Worker timed out after {self.timeout} seconds")
        return WorkerResult(
            completed.returncode, completed.stdout[-50000:], completed.stderr[-50000:]
        )
