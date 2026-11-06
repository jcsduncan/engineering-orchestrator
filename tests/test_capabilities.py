from __future__ import annotations

import subprocess
from dataclasses import asdict, replace

import pytest

from engineer.capabilities import (
    WorkerCapabilities,
    WorkerProfile,
    command_digest,
    require_mode,
    verify_worker_image,
)


def profile() -> WorkerProfile:
    return WorkerProfile(
        runtime="example-code",
        version="2.1.0",
        image_digest="sha256:" + "a" * 64,
        command_sha256=command_digest(("worker", "-p")),
        invocation="headless",
        capabilities=WorkerCapabilities(
            non_interactive="supported",
            subagents="supported",
            agent_teams="unverified",
            persistent_sessions="unsupported",
            tool_permissions="supported",
        ),
        evidence={
            "non_interactive": "probe/single.json",
            "subagents": "probe/subagents.json",
            "tool_permissions": "probe/permissions.json",
        },
    )


def test_profile_selects_only_verified_headless_modes() -> None:
    selected = profile()
    assert selected.snapshot("subagents")["mode"] == "subagents"
    with pytest.raises(ValueError, match="requires supported agent_teams"):
        selected.require("agent_teams")
    with pytest.raises(ValueError, match="verified worker profile"):
        require_mode("subagents", None)
    assert require_mode("single", None) == {"mode": "single", "source": "legacy-command"}


def test_profile_rejects_unverified_evidence_and_changed_schema() -> None:
    selected = profile()
    with pytest.raises(ValueError, match="needs probe evidence"):
        replace(selected, evidence={"non_interactive": "probe/single.json"})
    with pytest.raises(ValueError, match="only headless"):
        replace(selected, invocation="interactive")
    with pytest.raises(ValueError, match="immutable sha256"):
        replace(selected, image_digest="worker:latest")
    with pytest.raises(ValueError, match="Invalid worker profile fields"):
        WorkerProfile.from_dict({**asdict(selected), "extra": "ignored"})
    with pytest.raises(ValueError, match="Invalid worker capability fields"):
        WorkerProfile.from_dict(
            {**asdict(selected), "capabilities": {"non_interactive": "supported"}}
        )
    with pytest.raises(ValueError, match="command does not match"):
        selected.require_command(("different", "-p"))
    with pytest.raises(ValueError, match="Invalid worker execution mode"):
        require_mode([], selected)
    with pytest.raises(ValueError, match="nonempty argument vector"):
        command_digest(("worker", "bad\0argument"))


def test_local_image_must_match_profile_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    selected = profile()

    def inspect(*args, **kwargs):
        assert args[0][:4] == ["docker", "image", "inspect", "worker:local"]
        return subprocess.CompletedProcess(args[0], 0, stdout="sha256:" + "b" * 64 + "\n")

    monkeypatch.setattr("engineer.capabilities.subprocess.run", inspect)
    with pytest.raises(ValueError, match="does not match"):
        verify_worker_image("worker:local", selected)

    monkeypatch.setattr(
        "engineer.capabilities.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=selected.image_digest + "\n"
        ),
    )
    verify_worker_image("worker:local", selected)
