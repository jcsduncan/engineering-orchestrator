from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from .providers import ProviderInfo, WorkerResult


@dataclass(frozen=True)
class ContainerResult:
    exit_code: int | None
    stdout: str
    stderr: str


def _drain(stream, tail: bytearray, limit: int) -> None:
    while chunk := stream.read(8192):
        tail.extend(chunk)
        if len(tail) > limit:
            del tail[:-limit]


def _mount(path: Path, *, target: Path | None = None, readonly: bool = False) -> str:
    source = str(path)
    destination = str(target or path)
    if any(char in value for value in (source, destination) for char in (",", "\n")):
        raise ValueError("Container mount path contains an unsupported character")
    mode = ",readonly" if readonly else ""
    return f"type=bind,source={source},target={destination}{mode}"


@dataclass(frozen=True)
class ContainerRuntime:
    image: str
    git_metadata: Path
    cpus: int = 2
    memory_mb: int = 2048
    pids: int = 128
    network: str = "none"

    def __post_init__(self) -> None:
        if (
            not self.image
            or self.image.startswith("-")
            or any(char.isspace() for char in self.image)
        ):
            raise ValueError("Container image is required")
        if any(
            type(value) is not int or value <= 0 for value in (self.cpus, self.memory_mb, self.pids)
        ):
            raise ValueError("Container CPU, memory, and process limits must be positive integers")
        if self.network not in {"none", "bridge"}:
            raise ValueError("Container network must be none or bridge")

    def resolve_image(self) -> ContainerRuntime:
        """Pin a locally available image to its immutable ID for the whole run."""
        completed = subprocess.run(
            ["docker", "image", "inspect", self.image, "--format", "{{.Id}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        image_id = completed.stdout.strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("Docker returned an invalid image ID")
        return replace(self, image=image_id)

    def _command(
        self,
        argv: list[str],
        worktree: Path,
        run_id: str,
        name: str,
        safe_config: Path,
        worktree_configs: tuple[Path, ...] = (),
    ) -> list[str]:
        if not argv or not argv[0]:
            raise ValueError("Container command is required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
            raise ValueError("Invalid run ID for container name")
        worktree = worktree.resolve(strict=True)
        if not worktree.is_dir():
            raise ValueError("Run worktree must be a directory")
        git_dir = self.git_metadata.resolve(strict=True)
        if not git_dir.is_dir():
            raise ValueError("Git metadata must be a directory")
        command = [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--init",
            "--name",
            name,
            "--label",
            f"engineering-orchestrator.run={run_id}",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--network",
            self.network,
            "--cpus",
            str(self.cpus),
            "--memory",
            f"{self.memory_mb}m",
            "--memory-swap",
            f"{self.memory_mb}m",
            "--pids-limit",
            str(self.pids),
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=256m",
            "--mount",
            _mount(worktree),
            "--mount",
            _mount(git_dir, readonly=True),
            "--mount",
            _mount(safe_config, target=git_dir / "config", readonly=True),
        ]
        for config in worktree_configs:
            command.extend(["--mount", _mount(safe_config, target=config, readonly=True)])
        command.extend(
            [
                "--workdir",
                str(worktree),
                "--env",
                "HOME=/tmp",
                "--env",
                "UV_CACHE_DIR=/tmp/uv-cache",
                "--env",
                "GIT_OPTIONAL_LOCKS=0",
                "--env",
                f"ENGINEER_RUN_ID={run_id}",
            ]
        )
        if self.network == "bridge":
            command.extend(["--add-host", "host.docker.internal:host-gateway"])
        command.extend(["--entrypoint", argv[0], self.image, *argv[1:]])
        return command

    def run(
        self, argv: list[str], worktree: Path, run_id: str, timeout: int, output_limit: int
    ) -> ContainerResult:
        if type(timeout) is not int or timeout <= 0:
            raise ValueError("Container timeout must be a positive integer")
        if type(output_limit) is not int or output_limit <= 0:
            raise ValueError("Container output limit must be a positive integer")
        name = f"engineer-{run_id}-{uuid.uuid4().hex[:8]}"
        with tempfile.TemporaryDirectory(prefix="engineer-git-config-") as directory:
            safe_config = Path(directory) / "config"
            git_dir = self.git_metadata.resolve(strict=True)
            if (git_dir / "config").is_symlink():
                raise ValueError("Git config cannot be a symlink")
            object_format = subprocess.run(
                ["git", f"--git-dir={git_dir}", "rev-parse", "--show-object-format"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            if object_format not in {"sha1", "sha256"}:
                raise ValueError("Unsupported Git object format")
            config_text = "[core]\n\trepositoryformatversion = 0\n"
            if object_format == "sha256":
                config_text = (
                    "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha256\n"
                )
            safe_config.write_text(config_text)
            worktree_configs = tuple((git_dir / "worktrees").glob("*/config.worktree"))
            if any(config.is_symlink() for config in worktree_configs):
                raise ValueError("Git worktree config cannot be a symlink")
            command = self._command(argv, worktree, run_id, name, safe_config, worktree_configs)
            return self._invoke(command, name, timeout, output_limit)

    def _invoke(
        self, command: list[str], name: str, timeout: int, output_limit: int
    ) -> ContainerResult:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.stdout is not None and process.stderr is not None
        out_tail, err_tail = bytearray(), bytearray()
        readers = [
            threading.Thread(
                target=_drain, args=(process.stdout, out_tail, output_limit), daemon=True
            ),
            threading.Thread(
                target=_drain, args=(process.stderr, err_tail, output_limit), daemon=True
            ),
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        cleanup_error = None
        try:
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = None
        finally:
            # Removing a named container kills its process tree even if the Docker CLI exits first.
            try:
                removal = subprocess.run(
                    ["docker", "rm", "-f", name], capture_output=True, check=False, timeout=10
                )
                if removal.returncode != 0:
                    remaining = subprocess.run(
                        [
                            "docker",
                            "ps",
                            "-a",
                            "--filter",
                            f"name=^/{name}$",
                            "--format",
                            "{{.ID}}",
                        ],
                        capture_output=True,
                        check=True,
                        text=True,
                        timeout=10,
                    )
                    if remaining.stdout.strip():
                        raise RuntimeError("Container is still running after cleanup")
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                cleanup_error = exc
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            for reader in readers:
                reader.join(timeout=10)
        if cleanup_error is not None:
            raise RuntimeError("Could not confirm container removal") from cleanup_error
        stdout = out_tail.decode(errors="replace")
        stderr = err_tail.decode(errors="replace")
        if timed_out:
            stderr = (stderr + f"\nContainer timed out after {timeout} seconds")[-output_limit:]
        return ContainerResult(exit_code, stdout, stderr)


@dataclass(frozen=True)
class ContainerWorker:
    runtime: ContainerRuntime
    command: tuple[str, ...]
    timeout: int = 1800

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("A coding worker command is required")
        if type(self.timeout) is not int or self.timeout <= 0:
            raise ValueError("Worker timeout must be positive")

    @property
    def info(self) -> ProviderInfo:
        command_hash = hashlib.sha256("\0".join(self.command).encode()).hexdigest()
        return ProviderInfo(
            "container-command",
            {
                "image": self.runtime.image,
                "executable": Path(self.command[0]).name,
                "command_sha256": command_hash,
                "timeout_seconds": self.timeout,
                "cpus": self.runtime.cpus,
                "memory_mb": self.runtime.memory_mb,
                "pids": self.runtime.pids,
                "network": self.runtime.network,
            },
        )

    def execute(self, prompt: str, worktree: Path, run_id: str) -> WorkerResult:
        result = self.runtime.run([*self.command, prompt], worktree, run_id, self.timeout, 50000)
        return WorkerResult(result.exit_code, result.stdout, result.stderr)


@dataclass(frozen=True)
class ContainerCheckRunner:
    runtime: ContainerRuntime

    def execute(
        self, command: list[str], worktree: Path, run_id: str, timeout: int
    ) -> WorkerResult:
        result = self.runtime.run(command, worktree, run_id, timeout, 20000)
        return WorkerResult(result.exit_code, result.stdout, result.stderr)
