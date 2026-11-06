"""Version-bound capabilities for one opaque worker runtime."""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import asdict, dataclass

CAPABILITY_STATES = {"supported", "unsupported", "unverified"}
WORKER_MODES = {"single", "subagents", "agent_teams"}
IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class WorkerCapabilities:
    non_interactive: str
    subagents: str
    agent_teams: str
    persistent_sessions: str
    tool_permissions: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or value not in CAPABILITY_STATES
            for value in asdict(self).values()
        ):
            raise ValueError("Worker capabilities must be supported, unsupported, or unverified")


@dataclass(frozen=True)
class WorkerProfile:
    runtime: str
    version: str
    image_digest: str
    command_sha256: str
    invocation: str
    capabilities: WorkerCapabilities
    evidence: dict[str, str]

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.runtime, self.version)
        ):
            raise ValueError("Worker runtime and version are required")
        if not isinstance(self.image_digest, str) or not IMAGE_DIGEST.fullmatch(self.image_digest):
            raise ValueError("Worker image digest must be an immutable sha256 ID")
        if not isinstance(self.command_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.command_sha256
        ):
            raise ValueError("Worker command hash must be a sha256 hex digest")
        if self.invocation != "headless":
            raise ValueError("The current worker adapter supports only headless invocation")
        if not isinstance(self.capabilities, WorkerCapabilities):
            raise ValueError("Worker capabilities are required")
        if not isinstance(self.evidence, dict) or any(
            key not in asdict(self.capabilities) or not isinstance(value, str) or not value.strip()
            for key, value in self.evidence.items()
        ):
            raise ValueError("Worker capability evidence is invalid")
        for name, status in asdict(self.capabilities).items():
            if status == "supported" and name not in self.evidence:
                raise ValueError(f"Supported worker capability needs probe evidence: {name}")

    @classmethod
    def from_dict(cls, value: dict) -> WorkerProfile:
        """Reject incomplete or unknown declaration keys."""
        if not isinstance(value, dict) or set(value) != {
            "runtime",
            "version",
            "image_digest",
            "command_sha256",
            "invocation",
            "capabilities",
            "evidence",
        }:
            raise ValueError("Invalid worker profile fields")
        capabilities = value["capabilities"]
        if not isinstance(capabilities, dict) or set(capabilities) != set(
            WorkerCapabilities.__dataclass_fields__
        ):
            raise ValueError("Invalid worker capability fields")
        return cls(
            runtime=value["runtime"],
            version=value["version"],
            image_digest=value["image_digest"],
            command_sha256=value["command_sha256"],
            invocation=value["invocation"],
            capabilities=WorkerCapabilities(**capabilities),
            evidence=value["evidence"],
        )

    def require(self, mode: str) -> None:
        """Allow only modes supported by this exact headless runtime."""
        if not isinstance(mode, str) or mode not in WORKER_MODES:
            raise ValueError("Invalid worker execution mode")
        required = ["non_interactive", "tool_permissions"]
        if mode != "single":
            required.append(mode)
        for name in required:
            if getattr(self.capabilities, name) != "supported":
                raise ValueError(f"Worker mode {mode} requires supported {name}")

    def snapshot(self, mode: str) -> dict:
        self.require(mode)
        return {**asdict(self), "mode": mode}

    def require_command(self, command: tuple[str, ...] | list[str]) -> None:
        """Bind a profile to the exact argument vector used in its probe."""
        actual = command_digest(command)
        if actual != self.command_sha256:
            raise ValueError("Worker command does not match verified capability profile")


def command_digest(command: tuple[str, ...] | list[str]) -> str:
    """Hash the argument vector without shell parsing or ambiguous separators."""
    if not command or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command):
        raise ValueError("Worker command must be a nonempty argument vector")
    return hashlib.sha256("\0".join(command).encode()).hexdigest()


def require_mode(mode: str, profile: WorkerProfile | None) -> dict:
    """Preserve legacy single-worker use while gating explicit advanced modes."""
    if not isinstance(mode, str) or mode not in WORKER_MODES:
        raise ValueError("Invalid worker execution mode")
    if profile is None:
        if mode != "single":
            raise ValueError("Subagent and team modes require a verified worker profile")
        return {"mode": "single", "source": "legacy-command"}
    if not isinstance(profile, WorkerProfile):
        raise ValueError("Worker profile is invalid")
    return profile.snapshot(mode)


def verify_worker_image(image: str, profile: WorkerProfile) -> None:
    """Confirm the local image resolves to the digest tested by the profile."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Cannot inspect configured worker image") from exc
    if result.stdout.strip() != profile.image_digest:
        raise ValueError("Worker image does not match verified capability profile")
