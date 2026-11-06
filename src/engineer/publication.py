"""Publish one validated implementation run through a scoped GitHub installation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .core import _acquire_run_lock, _registered_git_dir, git
from .intake import IssueClaimStore
from .state import RunStore
from .validation import diff_sha256


@dataclass(frozen=True)
class PublicationIssue:
    run_id: str
    installation_id: int
    repository_id: int
    owner: str
    repository: str
    issue_number: int
    title: str

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.run_id
        ):
            raise ValueError("Invalid publication run ID")
        if any(
            type(value) is not int or value <= 0
            for value in (self.installation_id, self.repository_id, self.issue_number)
        ):
            raise ValueError("Invalid publication issue identity")
        for component in (self.owner, self.repository):
            if (
                not isinstance(component, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+", component)
                or component in {".", ".."}
            ):
                raise ValueError("Invalid publication repository path")


@dataclass(frozen=True)
class Publication:
    run_id: str
    branch: str
    commit_sha: str
    pull_number: int
    pull_url: str


class PublicationRemote(Protocol):
    def verify_repository(self, issue: PublicationIssue, base_sha: str) -> str: ...

    def branch_sha(self, issue: PublicationIssue, branch: str) -> str | None: ...

    def find_pull(self, issue: PublicationIssue, branch: str, base: str) -> dict | None: ...

    def get_pull(self, issue: PublicationIssue, number: int) -> dict: ...

    def create_pull(
        self, issue: PublicationIssue, branch: str, base: str, title: str, body: str
    ) -> dict: ...

    def push(self, issue: PublicationIssue, worktree: Path, git_dir: Path, branch: str) -> None: ...


class GitHubPublicationRemote:
    """Use a one-repository GitHub App installation token for publication only."""

    def __init__(self, token: str, *, timeout: int = 20) -> None:
        if not token:
            raise ValueError("A GitHub App installation token is required")
        if type(timeout) is not int or timeout <= 0:
            raise ValueError("GitHub request timeout must be positive")
        self._token = token
        self._timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None = None) -> object:
        request = Request(
            f"https://api.github.com{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2026-03-10",
                "Content-Type": "application/json",
                "User-Agent": "engineering-orchestrator",
            },
        )
        with urlopen(request, timeout=self._timeout) as response:
            return json.load(response)

    @staticmethod
    def _repo_path(issue: PublicationIssue) -> str:
        return f"/repos/{quote(issue.owner, safe='')}/{quote(issue.repository, safe='')}"

    def verify_repository(self, issue: PublicationIssue, base_sha: str) -> str:
        scope = self._request("GET", "/installation/repositories?per_page=100")
        repositories = scope["repositories"]
        if scope["total_count"] != 1 or len(repositories) != 1:
            raise ValueError("Publication token must be scoped to one repository")
        allowed = repositories[0]
        expected = f"{issue.owner}/{issue.repository}"
        if (
            allowed["id"] != issue.repository_id
            or allowed["full_name"].casefold() != expected.casefold()
        ):
            raise ValueError("Publication token does not match the claimed repository")
        repository = self._request("GET", self._repo_path(issue))
        if repository["id"] != issue.repository_id:
            raise ValueError("Claimed repository ID no longer matches GitHub")
        self._request("GET", f"{self._repo_path(issue)}/git/commits/{base_sha}")
        return repository["default_branch"]

    def branch_sha(self, issue: PublicationIssue, branch: str) -> str | None:
        try:
            reference = self._request("GET", f"{self._repo_path(issue)}/git/ref/heads/{branch}")
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return reference["object"]["sha"]

    def find_pull(self, issue: PublicationIssue, branch: str, base: str) -> dict | None:
        query = urlencode({"state": "all", "head": f"{issue.owner}:{branch}", "per_page": 100})
        pulls = self._request("GET", f"{self._repo_path(issue)}/pulls?{query}")
        if len(pulls) > 1:
            raise ValueError("Multiple pull requests exist for this run branch")
        if not pulls:
            return None
        if pulls[0]["base"]["ref"] != base:
            raise ValueError("Existing pull request targets another base branch")
        if pulls[0]["state"] == "closed" and not pulls[0].get("merged_at"):
            raise ValueError("Existing pull request was closed without merging")
        return pulls[0]

    def create_pull(
        self, issue: PublicationIssue, branch: str, base: str, title: str, body: str
    ) -> dict:
        return self._request(
            "POST",
            f"{self._repo_path(issue)}/pulls",
            {"head": branch, "base": base, "title": title, "body": body},
        )

    def get_pull(self, issue: PublicationIssue, number: int) -> dict:
        return self._request("GET", f"{self._repo_path(issue)}/pulls/{number}")

    def push(self, issue: PublicationIssue, worktree: Path, git_dir: Path, branch: str) -> None:
        with tempfile.TemporaryDirectory(prefix="engineer-publish-") as directory:
            askpass = Path(directory) / "askpass.sh"
            askpass.write_text(
                '#!/bin/sh\ncase "$1" in\n'
                "  *Username*) printf %s x-access-token ;;\n"
                '  *Password*) printf %s "$ENGINEER_GITHUB_TOKEN" ;;\n'
                "  *) exit 1 ;;\n"
                "esac\n"
            )
            askpass.chmod(0o700)
            environment = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": directory,
                "GIT_ASKPASS": str(askpass),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "ENGINEER_GITHUB_TOKEN": self._token,
            }
            url = f"https://x-access-token@github.com/{issue.owner}/{issue.repository}.git"
            result = subprocess.run(
                [
                    "git",
                    f"--git-dir={git_dir}",
                    f"--work-tree={worktree}",
                    "-c",
                    "credential.helper=",
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "push",
                    url,
                    f"HEAD:refs/heads/{branch}",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
                env=environment,
            )
            if result.returncode != 0:
                raise RuntimeError(f"GitHub branch push failed (exit {result.returncode})")


def _git_command(worktree: Path, git_dir: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            f"--git-dir={git_dir}",
            f"--work-tree={worktree}",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "commit.gpgSign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _changed_manifest(worktree: Path, git_dir: Path) -> str:
    """Hash changed file bytes independently of index staging."""
    paths = set()
    for args in (
        ("diff", "--name-only", "--no-ext-diff", "--no-textconv", "--no-renames", "-z", "HEAD"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        result = subprocess.run(
            ["git", f"--git-dir={git_dir}", f"--work-tree={worktree}", *args],
            capture_output=True,
            check=True,
        )
        paths.update(path for path in result.stdout.split(b"\0") if path)
    digest = hashlib.sha256()
    for raw in sorted(paths):
        path = worktree / os.fsdecode(raw)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
        if not path.exists() and not path.is_symlink():
            digest.update(b"deleted")
        else:
            mode = path.lstat().st_mode
            digest.update(str(stat.S_IMODE(mode)).encode())
            if stat.S_ISLNK(mode):
                digest.update(os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(mode):
                with path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
            else:
                raise ValueError(f"Unsupported changed entry: {raw!r}")
    return digest.hexdigest()


def _body(issue: PublicationIssue, record: dict, title: str) -> str:
    result = record["result"]
    checks = result["checks"]
    check_lines = [
        f"- {json.dumps(item['command'])} — {item['status']} (exit {item['exit_code']})"
        for item in checks
    ]
    risks = result["plan"].get("risks", [])
    lines = [
        f"Closes #{issue.issue_number}",
        "",
        "## Run evidence",
        f"- Run ID: `{issue.run_id}`",
        f"- Base SHA: `{record['base_sha']}`",
        f"- Validated diff SHA-256: `{result['validation']['diff_sha256_after']}`",
        "",
        "## Checks",
        *(check_lines or ["- No checks recorded"]),
        "",
        "## Risks",
        *([f"- {json.dumps(risk)}" for risk in risks] or ["- None recorded by the planner"]),
        "",
        "## Unverified work",
        "- No validation beyond the listed commands is claimed.",
    ]
    if title.startswith(("feat: ", "fix: ")):
        lines.extend(["", "BEGIN_COMMIT_OVERRIDE", title, "END_COMMIT_OVERRIDE"])
    return "\n".join(lines)


def _publication_db(store: RunStore) -> None:
    with store.db:
        store.db.execute(
            """CREATE TABLE IF NOT EXISTS run_publications (
                run_id TEXT PRIMARY KEY REFERENCES runs(id),
                installation_id INTEGER NOT NULL,
                repository_id INTEGER NOT NULL,
                issue_number INTEGER NOT NULL,
                commit_sha TEXT NOT NULL,
                pull_number INTEGER,
                pull_url TEXT
            )"""
        )


def _verify_claim(state_dir: Path, issue: PublicationIssue) -> None:
    claims = IssueClaimStore(state_dir / "runs.sqlite3")
    try:
        claim = claims.get(issue.run_id)
    finally:
        claims.close()
    if claim is None or claim.status not in {"claimed", "completed"}:
        raise ValueError("Run has no active or completed issue claim")
    snapshot = claim.snapshot
    if (
        snapshot.installation_id != issue.installation_id
        or snapshot.repository_id != issue.repository_id
        or snapshot.owner != issue.owner
        or snapshot.repository != issue.repository
        or snapshot.issue_number != issue.issue_number
        or snapshot.title != issue.title
    ):
        raise ValueError("Publication issue does not match the stored claim")


def publish_run(
    *, state_dir: Path, issue: PublicationIssue, title: str, remote: PublicationRemote
) -> Publication:
    """Publish or recover one validated run without creating a duplicate PR."""
    if (
        not isinstance(title, str)
        or len(title) > 200
        or not re.fullmatch(r"(?:feat|fix|docs|refactor|perf|test|build|ci|chore): [^\r\n]+", title)
    ):
        raise ValueError("Pull request title must use a conventional subject")
    state_dir = state_dir.expanduser().resolve()
    lock = _acquire_run_lock(state_dir, issue.run_id)
    try:
        _verify_claim(state_dir, issue)
        store = RunStore(state_dir / "runs.sqlite3")
        try:
            _publication_db(store)
            record = store.get(issue.run_id)
            if (
                record is None
                or record["status"] != "ready_for_review"
                or record["mode"] != "implementation"
            ):
                raise ValueError("Only validated implementation runs can be published")
            result = record["result"]
            validation = result["validation"]
            if (
                validation["status"] != "passed"
                or not result["checks"]
                or validation["diff_sha256_before"] != validation["diff_sha256_after"]
                or not result["changed_files"]
            ):
                raise ValueError("Run has no validated implementation diff")
            worktree = Path(record["worktree"])
            repo = Path(record["repo"])
            if git(repo, "rev-parse", "--show-toplevel") != str(repo):
                raise ValueError("Run repository path has changed")
            git_dir = _registered_git_dir(repo, worktree)
            if git_dir is None:
                raise ValueError("Run worktree metadata is invalid")
            actual_branch = git(worktree, "symbolic-ref", "--short", "HEAD", git_dir=git_dir)
            if actual_branch != record["branch"] or actual_branch != f"agent/{issue.run_id}":
                raise ValueError("Run branch has changed")
            base = remote.verify_repository(issue, record["base_sha"])
            branch = record["branch"]
            head = git(worktree, "rev-parse", "HEAD", git_dir=git_dir)
            if head == record["base_sha"]:
                if diff_sha256(worktree, git_dir=git_dir) != validation["diff_sha256_after"]:
                    raise ValueError("Worktree no longer matches validated diff")
                manifest = _changed_manifest(worktree, git_dir)
                index = git_dir / "index"
                original_index = index.read_bytes()
                try:
                    _git_command(worktree, git_dir, "add", "-A")
                    if _changed_manifest(worktree, git_dir) != manifest:
                        raise ValueError("Worktree changed while staging publication")
                    _git_command(
                        worktree,
                        git_dir,
                        "-c",
                        "user.name=Engineering Orchestrator",
                        "-c",
                        "user.email=orchestrator@users.noreply.github.com",
                        "commit",
                        "-m",
                        f"{title}\n\nRun-ID: {issue.run_id}",
                    )
                except Exception:
                    if git(worktree, "rev-parse", "HEAD", git_dir=git_dir) == record["base_sha"]:
                        with tempfile.NamedTemporaryFile(dir=git_dir, delete=False) as saved:
                            saved.write(original_index)
                        os.replace(saved.name, index)
                    raise
                head = git(worktree, "rev-parse", "HEAD", git_dir=git_dir)
            else:
                parent = git(worktree, "rev-parse", "HEAD^", git_dir=git_dir)
                message = git(worktree, "log", "-1", "--format=%B", git_dir=git_dir)
                footer = f"Run-ID: {issue.run_id}"
                if (
                    parent != record["base_sha"]
                    or not message.splitlines()
                    or message.splitlines()[-1] != footer
                ):
                    raise ValueError("Run branch contains an unrecognized commit")
            if git(worktree, "status", "--porcelain", "--untracked-files=all", git_dir=git_dir):
                raise ValueError("Run worktree is dirty after publication commit")
            existing = store.db.execute(
                """SELECT installation_id, repository_id, issue_number, commit_sha,
                pull_number, pull_url FROM run_publications WHERE run_id = ?""",
                (issue.run_id,),
            ).fetchone()
            if existing and (
                existing["commit_sha"] != head
                or existing["installation_id"] != issue.installation_id
                or existing["repository_id"] != issue.repository_id
                or existing["issue_number"] != issue.issue_number
            ):
                raise ValueError("Recorded publication does not match run or issue")
            with store.db:
                store.db.execute(
                    """INSERT OR IGNORE INTO run_publications
                    (run_id, installation_id, repository_id, issue_number, commit_sha)
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        issue.run_id,
                        issue.installation_id,
                        issue.repository_id,
                        issue.issue_number,
                        head,
                    ),
                )
            pull = (
                remote.get_pull(issue, existing["pull_number"])
                if existing and existing["pull_number"]
                else remote.find_pull(issue, branch, base)
            )
            if pull is not None:
                if pull["head"]["sha"] != head:
                    raise ValueError("Existing pull request points to a different commit")
                pull_body = pull.get("body") or ""
                if (
                    f"Run ID: `{issue.run_id}`" not in pull_body
                    or f"Closes #{issue.issue_number}" not in pull_body
                    or pull["base"]["ref"] != base
                ):
                    raise ValueError("Existing pull request does not belong to this run and issue")
                if pull["state"] == "closed" and not pull.get("merged_at"):
                    raise ValueError("Existing pull request was closed without merging")
            if pull is None or pull["state"] == "open":
                _verify_claim(state_dir, issue)
                remote_sha = remote.branch_sha(issue, branch)
                if remote_sha is None:
                    remote.push(issue, worktree, git_dir, branch)
                elif remote_sha != head:
                    raise ValueError("Remote run branch points to a different commit")
                if pull is None:
                    body = _body(issue, record, title)
                    pull = remote.create_pull(issue, branch, base, title, body)
            expected_url = f"https://github.com/{issue.owner}/{issue.repository}/pull/"
            pull_body = pull.get("body") or ""
            if (
                pull["head"]["sha"] != head
                or pull["base"]["ref"] != base
                or f"Run ID: `{issue.run_id}`" not in pull_body
                or f"Closes #{issue.issue_number}" not in pull_body
                or type(pull["number"]) is not int
                or pull["number"] <= 0
                or not pull["html_url"].casefold().startswith(expected_url.casefold())
            ):
                raise ValueError("GitHub pull request response does not match this run")
            publication = Publication(issue.run_id, branch, head, pull["number"], pull["html_url"])
            with store.db:
                store.db.execute(
                    "UPDATE run_publications SET pull_number = ?, pull_url = ? WHERE run_id = ?",
                    (publication.pull_number, publication.pull_url, issue.run_id),
                )
            return publication
        finally:
            store.close()
    finally:
        lock.close()
