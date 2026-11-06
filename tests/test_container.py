import os
import shlex
import subprocess
import time
from io import BytesIO
from pathlib import Path

import pytest

from engineer.container import ContainerResult, ContainerRuntime
from engineer.core import run_task
from engineer.providers import Plan
from engineer.validation import diff_sha256


@pytest.fixture
def worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://user:secret@example.com/repo.git",
        ],
        check=True,
    )
    (repo / "README.md").write_text("before\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-qb", "task", str(checkout)], check=True
    )
    worktree_git_dir = Path(
        subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "--absolute-git-dir"], text=True
        ).strip()
    )
    (worktree_git_dir / "config.worktree").write_text(
        '[remote "private"]\n\turl = https://user:worktree-secret@example.com/repo.git\n'
    )
    return checkout


def test_container_command_limits_mounts_and_environment(worktree, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "host-secret")
    runtime = ContainerRuntime(
        "test-image",
        worktree.parent / "repo/.git",
        cpus=2,
        memory_mb=512,
        pids=64,
        network="bridge",
    )
    safe_config = worktree.parent / "safe-config"
    safe_config.write_text("[core]\n")
    command = runtime._command(
        ["python", "-c", "print(1)"], worktree, "run-1", "test-name", safe_config
    )
    rendered = " ".join(command)
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert "--network bridge" in rendered
    assert "--memory 512m" in rendered
    assert "--memory-swap 512m" in rendered
    assert "--pids-limit 64" in rendered
    assert "host.docker.internal:host-gateway" in command
    assert "host-secret" not in rendered
    assert "GH_TOKEN" not in rendered
    mounts = [command[index + 1] for index, item in enumerate(command[:-1]) if item == "--mount"]
    assert mounts[0] == f"type=bind,source={worktree},target={worktree}"
    assert mounts[1].endswith(",readonly")
    assert mounts[2] == (
        f"type=bind,source={safe_config},target={worktree.parent / 'repo/.git/config'},readonly"
    )
    assert "/var/run/docker.sock" not in rendered
    assert command[-4:] == ["python", "test-image", "-c", "print(1)"]


def test_git_metadata_mount_does_not_follow_worktree_pointer(worktree):
    runtime = ContainerRuntime("test-image", worktree.parent / "repo/.git")
    (worktree / ".git").write_text("gitdir: /etc\n")
    safe_config = worktree.parent / "safe-config"
    safe_config.write_text("[core]\n")
    command = runtime._command(["python", "--version"], worktree, "run-1", "test-name", safe_config)
    mounts = [command[index + 1] for index, item in enumerate(command[:-1]) if item == "--mount"]
    assert mounts[1] == (
        f"type=bind,source={worktree.parent / 'repo/.git'},"
        f"target={worktree.parent / 'repo/.git'},readonly"
    )


def test_oracle_mounts_worktree_readonly(worktree):
    runtime = ContainerRuntime("test-image", worktree.parent / "repo/.git")
    safe_config = worktree.parent / "safe-config"
    safe_config.write_text("[core]\n")
    command = runtime._command(
        ["python", "-B", "-c", "pass"],
        worktree,
        "run-1",
        "test-name",
        safe_config,
        readonly_worktree=True,
    )
    mounts = [command[index + 1] for index, item in enumerate(command[:-1]) if item == "--mount"]
    assert mounts[0] == f"type=bind,source={worktree},target={worktree},readonly"


def test_host_diff_uses_pinned_git_dir(worktree):
    git_dir = Path(
        subprocess.check_output(
            ["git", "-C", str(worktree), "rev-parse", "--absolute-git-dir"], text=True
        ).strip()
    )
    expected = diff_sha256(worktree, git_dir=git_dir)
    (worktree / ".git").write_text("gitdir: /etc\n")
    assert diff_sha256(worktree, git_dir=git_dir) == expected


@pytest.mark.parametrize("run_id", ["Resume_ABC123", "a" * 64])
def test_container_accepts_resume_run_ids(worktree, run_id):
    runtime = ContainerRuntime("test-image", worktree.parent / "repo/.git")
    safe_config = worktree.parent / "safe-config"
    safe_config.write_text("[core]\n")
    command = runtime._command(["python", "--version"], worktree, run_id, "test-name", safe_config)
    assert f"engineering-orchestrator.run={run_id}" in command


@pytest.mark.parametrize("run_id", ["_invalid", "a" * 65, "a/b"])
def test_container_rejects_invalid_run_ids(worktree, run_id):
    runtime = ContainerRuntime("test-image", worktree.parent / "repo/.git")
    with pytest.raises(ValueError, match="Invalid run ID"):
        runtime._command(
            ["python", "--version"], worktree, run_id, "test-name", Path("/tmp/config")
        )


def test_container_rejects_symlinked_git_config(worktree, tmp_path):
    common_git_dir = worktree.parent / "repo/.git"
    config = common_git_dir / "config"
    config.rename(tmp_path / "original-config")
    config.symlink_to(tmp_path / "original-config")
    runtime = ContainerRuntime("test-image", common_git_dir)
    with pytest.raises(ValueError, match="Git config cannot be a symlink"):
        runtime.run(["python", "--version"], worktree, "run-1", 5, 100)


@pytest.mark.parametrize(
    "kwargs",
    [{"cpus": 0}, {"memory_mb": 0}, {"pids": 0}, {"network": "host"}],
)
def test_invalid_container_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        ContainerRuntime("image", Path("/tmp"), **kwargs)


@pytest.mark.parametrize("auto_removing", [False, True])
def test_runner_bounds_output_and_removes_container(worktree, monkeypatch, auto_removing):
    import engineer.container as container

    commands = []

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, "sha1\n", "")
        if command[:3] == ["docker", "rm", "-f"]:
            commands.append(command)
            return subprocess.CompletedProcess(command, int(auto_removing), b"", b"")
        if command[:2] == ["docker", "ps"]:
            commands.append(command)
            stdout = "container-id\n" if commands.count(command) == 1 else ""
            return subprocess.CompletedProcess(command, 0, stdout, "")
        pytest.fail(f"Unexpected command: {command}")

    class Process:
        stdout = BytesIO(b"x" * 4000)
        stderr = BytesIO(b"y" * 4000)

        def wait(self, timeout):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(container.subprocess, "run", fake_run)
    monkeypatch.setattr(container.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    result = ContainerRuntime("image", worktree.parent / "repo/.git").run(
        ["python", "--version"], worktree, "run-1", 5, 100
    )
    assert result == ContainerResult(0, "x" * 100, "y" * 100)
    assert commands and commands[0][0:3] == ["docker", "rm", "-f"]
    if auto_removing:
        assert len(commands) == 3


@pytest.mark.parametrize("cleanup_mode", ["timeout", "still_running"])
def test_timeout_kills_docker_cli_when_cleanup_fails(worktree, monkeypatch, cleanup_mode):
    import engineer.container as container

    class Process:
        stdout = BytesIO(b"")
        stderr = BytesIO(b"")
        waits = 0
        killed = False

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired(["docker", "run"], timeout)
            return -9

        def poll(self):
            return None

        def kill(self):
            self.killed = True

    process = Process()
    if cleanup_mode == "still_running":
        monkeypatch.setattr(container, "_REMOVAL_WAIT_SECONDS", 0)

    def fail_cleanup(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, "sha1\n", "")
        if cleanup_mode == "timeout":
            raise subprocess.TimeoutExpired(["docker", "rm"], 10)
        if command[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(command, 1, b"", b"error")
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(command, 0, "container-id\n", "")
        pytest.fail(f"Unexpected command: {command}")

    monkeypatch.setattr(container.subprocess, "run", fail_cleanup)
    monkeypatch.setattr(container.subprocess, "Popen", lambda *_args, **_kwargs: process)
    with pytest.raises(RuntimeError, match="Could not confirm container removal"):
        ContainerRuntime("image", worktree.parent / "repo/.git").run(
            ["python", "--version"], worktree, "run-1", 1, 100
        )
    assert process.killed


@pytest.mark.skipif(not os.getenv("ENGINEER_TEST_IMAGE"), reason="No local test image configured")
def test_container_isolation_and_timeout(worktree, tmp_path, monkeypatch):
    image = os.environ["ENGINEER_TEST_IMAGE"]
    runtime = ContainerRuntime(image, worktree.parent / "repo/.git").resolve_image()
    publication_credentials = tmp_path / "controller" / "gh" / "hosts.yml"
    publication_credentials.parent.mkdir(parents=True)
    publication_credentials.write_text("oauth_token: host-secret\n")
    monkeypatch.setenv("GH_TOKEN", "host-secret")
    script = f"""
import os
import subprocess
import errno
from pathlib import Path
assert os.getenv('GH_TOKEN') is None
assert not Path('/var/run/docker.sock').exists()
assert not Path({str(publication_credentials)!r}).exists()
assert subprocess.check_output(['git', 'status', '--short']) == b''
metadata_text = subprocess.check_output(
    ['git', 'rev-parse', '--absolute-git-dir'], text=True
).strip()
metadata = Path(metadata_text)
assert 'secret' not in (metadata.parents[1] / 'config').read_text()
assert 'worktree-secret' not in (metadata / 'config.worktree').read_text()
try:
    (metadata / 'probe').write_text('x')
except OSError as exc:
    assert exc.errno in {{errno.EACCES, errno.EROFS}}
else:
    raise AssertionError('Git metadata was writable')
Path('README.md').write_text('after\\n')
"""
    result = runtime.run(["python", "-c", script], worktree, "boundary-test", 30, 2000)
    assert result.exit_code == 0, result.stderr
    assert (worktree / "README.md").read_text() == "after\n"

    child_script = (
        "import time; from pathlib import Path; "
        "Path('child-started').write_text('yes'); "
        "time.sleep(4); Path('child-escaped').write_text('yes')"
    )
    parent_script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
        "time.sleep(30)"
    )
    timeout = runtime.run(["python", "-c", parent_script], worktree, "timeout-test", 2, 2000)
    assert timeout.exit_code is None
    assert "timed out" in timeout.stderr
    assert (worktree / "child-started").exists()
    time.sleep(2.5)
    assert not (worktree / "child-escaped").exists()
    remaining = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=engineering-orchestrator.run=timeout-test",
            "--format",
            "{{.ID}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not remaining.stdout.strip()


@pytest.mark.skipif(not os.getenv("ENGINEER_TEST_IMAGE"), reason="No local test image configured")
def test_oracle_container_cannot_modify_worktree(worktree):
    runtime = ContainerRuntime(
        os.environ["ENGINEER_TEST_IMAGE"], worktree.parent / "repo/.git"
    ).resolve_image()
    result = runtime.run(
        ["python", "-B", "-c", "from pathlib import Path; Path('README.md').write_text('changed')"],
        worktree,
        "oracle-readonly",
        30,
        2000,
        readonly_worktree=True,
    )
    assert result.exit_code != 0
    assert (worktree / "README.md").read_text() == "before\n"


@pytest.mark.skipif(not os.getenv("ENGINEER_TEST_IMAGE"), reason="No local test image configured")
def test_run_task_executes_worker_and_checks_in_container(worktree, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "engineer.providers.OpenAICompatiblePlanner.plan",
        lambda _planner, _task: Plan("Update", ["Edit"], ["Check"], []),
    )
    monkeypatch.setenv("GH_TOKEN", "host-secret")
    worker_script = (
        "import os; from pathlib import Path; "
        "assert os.getenv('GH_TOKEN') is None; "
        "Path('README.md').write_text('container\\n')"
    )
    check_script = (
        "import os; from pathlib import Path; "
        "assert os.getenv('GH_TOKEN') is None; "
        "assert Path('README.md').read_text() == 'container\\n'"
    )
    run_options = dict(
        task="Update readme",
        repo=worktree.parent / "repo",
        model="local-test",
        base_url="http://localhost:11434/v1",
        worker=["python", "-c", worker_script, "ignored"],
        checks=[shlex.join(["python", "-c", check_script])],
        state_dir=tmp_path / "state",
        execution_image=os.environ["ENGINEER_TEST_IMAGE"],
        run_id="Resume_ABC123",
    )
    result = run_task(**run_options)
    assert result["status"] == "ready_for_review"
    assert result["providers"]["worker"]["name"] == "container-command"
    assert result["checks"][0]["exit_code"] == 0
    assert run_task(**run_options) == result
