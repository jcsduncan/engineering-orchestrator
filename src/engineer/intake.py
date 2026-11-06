"""Reconcile opt-in GitHub issues into durable, idempotent task claims."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
from urllib.request import Request, urlopen

RUN_LABEL = "agent:run"
_REPOSITORY_PART = re.compile(r"[A-Za-z0-9_.-]+\Z")


@dataclass(frozen=True)
class RepositoryConfig:
    installation_id: int
    owner: str
    name: str

    def __post_init__(self) -> None:
        if type(self.installation_id) is not int or self.installation_id <= 0:
            raise ValueError("installation_id must be a positive integer")
        if any(
            not _REPOSITORY_PART.fullmatch(part) or part in {".", ".."}
            for part in (self.owner, self.name)
        ):
            raise ValueError("owner and repository name must be GitHub path components")


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str
    labels: frozenset[str]
    url: str
    updated_at: str
    is_pull_request: bool = False


@dataclass(frozen=True)
class TaskSnapshot:
    run_id: str
    installation_id: int
    repository_id: int
    owner: str
    repository: str
    issue_number: int
    generation: int
    title: str
    body: str
    objective: str
    acceptance_criteria: str
    issue_url: str
    issue_updated_at: str


@dataclass(frozen=True)
class IssueClaim:
    snapshot: TaskSnapshot
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class ReconcileResult:
    claimed: list[IssueClaim]
    cancelled: list[IssueClaim]


class IssueSource(Protocol):
    """Read current issues and native GitHub blocking relationships."""

    def repository_id(self, repo: RepositoryConfig) -> int: ...

    def list_labelled(self, repo: RepositoryConfig) -> list[int]: ...

    def get_issue(self, repo: RepositoryConfig, number: int) -> Issue: ...

    def has_open_blocker(self, repo: RepositoryConfig, number: int) -> bool: ...


class GitHubIssueSource:
    """Read GitHub REST issues with a scoped installation token."""

    def __init__(self, token: str, *, timeout: int = 20) -> None:
        if not token:
            raise ValueError("A GitHub token is required")
        if timeout <= 0:
            raise ValueError("GitHub request timeout must be positive")
        self._token = token
        self._timeout = timeout

    def _get(self, path: str) -> object:
        request = Request(
            f"https://api.github.com{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2026-03-10",
            },
        )
        with urlopen(request, timeout=self._timeout) as response:
            return json.load(response)

    @staticmethod
    def _path(repo: RepositoryConfig) -> str:
        return f"/repos/{quote(repo.owner, safe='')}/{quote(repo.name, safe='')}"

    def repository_id(self, repo: RepositoryConfig) -> int:
        data = self._get(self._path(repo))
        return int(data["id"])

    def list_labelled(self, repo: RepositoryConfig) -> list[int]:
        numbers = []
        page = 1
        while True:
            issues = self._get(
                f"{self._path(repo)}/issues?state=open&labels={quote(RUN_LABEL)}"
                f"&per_page=100&page={page}"
            )
            numbers.extend(item["number"] for item in issues if "pull_request" not in item)
            if len(issues) < 100:
                return numbers
            page += 1

    def get_issue(self, repo: RepositoryConfig, number: int) -> Issue:
        data = self._get(f"{self._path(repo)}/issues/{number}")
        return Issue(
            number=data["number"],
            title=data["title"],
            body=data["body"] or "",
            state=data["state"],
            labels=frozenset(item["name"] for item in data["labels"]),
            url=data["html_url"],
            updated_at=data["updated_at"],
            is_pull_request="pull_request" in data,
        )

    def has_open_blocker(self, repo: RepositoryConfig, number: int) -> bool:
        page = 1
        while True:
            blockers = self._get(
                f"{self._path(repo)}/issues/{number}/dependencies/blocked_by"
                f"?per_page=100&page={page}"
            )
            if any(item["state"] == "open" for item in blockers):
                return True
            if len(blockers) < 100:
                return False
            page += 1


class IssueClaimStore:
    """Store task claims and cancellation decisions in SQLite."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS issue_claims (
                    installation_id INTEGER NOT NULL,
                    repository_id INTEGER NOT NULL,
                    issue_number INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    run_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK (status IN ('claimed', 'cancelled', 'completed')),
                    snapshot_json TEXT NOT NULL,
                    reason TEXT,
                    cancel_ack_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (installation_id, repository_id, issue_number, generation)
                )"""
            )
            self.db.execute(
                """CREATE INDEX IF NOT EXISTS issue_claims_active
                ON issue_claims (installation_id, repository_id, status)"""
            )

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _claim(row: sqlite3.Row) -> IssueClaim:
        return IssueClaim(
            TaskSnapshot(**json.loads(row["snapshot_json"])), row["status"], row["reason"]
        )

    def active(self, installation_id: int, repository_id: int) -> list[IssueClaim]:
        rows = self.db.execute(
            """SELECT * FROM issue_claims WHERE installation_id = ? AND repository_id = ?
            AND status = 'claimed' ORDER BY issue_number""",
            (installation_id, repository_id),
        )
        return [self._claim(row) for row in rows]

    def pending_cancellations(self, installation_id: int, repository_id: int) -> list[IssueClaim]:
        """Return revocations until a worker controller acknowledges them."""
        rows = self.db.execute(
            """SELECT * FROM issue_claims WHERE installation_id = ? AND repository_id = ?
            AND status = 'cancelled' AND cancel_ack_at IS NULL ORDER BY issue_number""",
            (installation_id, repository_id),
        )
        return [self._claim(row) for row in rows]

    def claim(self, snapshot: TaskSnapshot) -> IssueClaim | None:
        """Insert one immutable generation, excluding overlapping active generations."""
        key = (
            snapshot.installation_id,
            snapshot.repository_id,
            snapshot.issue_number,
            snapshot.generation,
        )
        now = int(time.time())
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            active = self.db.execute(
                """SELECT 1 FROM issue_claims WHERE installation_id = ? AND repository_id = ?
                AND issue_number = ? AND status = 'claimed'""",
                key[:3],
            ).fetchone()
            if active:
                return None
            cursor = self.db.execute(
                """INSERT INTO issue_claims
                (installation_id, repository_id, issue_number, generation, run_id,
                 status, snapshot_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'claimed', ?, ?, ?)
                ON CONFLICT (installation_id, repository_id, issue_number, generation)
                DO NOTHING""",
                (*key, snapshot.run_id, json.dumps(asdict(snapshot)), now, now),
            )
        return IssueClaim(snapshot, "claimed") if cursor.rowcount == 1 else None

    def cancel(self, run_id: str, reason: str) -> IssueClaim | None:
        """Revoke an unfinished claim once and retain its task snapshot."""
        with self.db:
            cursor = self.db.execute(
                """UPDATE issue_claims SET status = 'cancelled', reason = ?, updated_at = ?
                WHERE run_id = ? AND status = 'claimed'""",
                (reason, int(time.time()), run_id),
            )
            if cursor.rowcount == 0:
                return None
            row = self.db.execute(
                "SELECT * FROM issue_claims WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._claim(row)

    def complete(self, run_id: str) -> None:
        """Mark a dispatched claim complete so only unfinished work can be revoked."""
        with self.db:
            cursor = self.db.execute(
                """UPDATE issue_claims SET status = 'completed', updated_at = ?
                WHERE run_id = ? AND status = 'claimed'""",
                (int(time.time()), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Run has no active issue claim")

    def acknowledge_cancel(self, run_id: str) -> None:
        """Acknowledge a revocation after the run has stopped."""
        with self.db:
            cursor = self.db.execute(
                """UPDATE issue_claims SET cancel_ack_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'cancelled' AND cancel_ack_at IS NULL""",
                (int(time.time()), int(time.time()), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Run has no pending cancellation")


def _section(body: str, heading: str) -> str:
    lines = body.splitlines()
    start = next(
        (
            index + 1
            for index, line in enumerate(lines)
            if re.fullmatch(r"##\s+" + re.escape(heading) + r"\s*", line, flags=re.IGNORECASE)
        ),
        None,
    )
    if start is None:
        return ""
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines)
    )
    return "\n".join(lines[start:end]).strip()


def _snapshot(
    repo: RepositoryConfig, repository_id: int, issue: Issue, generation: int
) -> TaskSnapshot:
    key = [repo.installation_id, repository_id, issue.number, generation]
    run_id = "issue-" + hashlib.sha256(json.dumps(key).encode()).hexdigest()[:32]
    return TaskSnapshot(
        run_id=run_id,
        installation_id=repo.installation_id,
        repository_id=repository_id,
        owner=repo.owner,
        repository=repo.name,
        issue_number=issue.number,
        generation=generation,
        title=issue.title,
        body=issue.body,
        objective=_section(issue.body, "Outcome") or issue.title,
        acceptance_criteria=_section(issue.body, "Acceptance"),
        issue_url=issue.url,
        issue_updated_at=issue.updated_at,
    )


def reconcile(
    source: IssueSource,
    store: IssueClaimStore,
    repo: RepositoryConfig,
    *,
    generation: int = 1,
    issue_number: int | None = None,
) -> ReconcileResult:
    """Claim current eligible issues and revoke unfinished claims that lost opt-in."""
    if type(generation) is not int or generation <= 0:
        raise ValueError("generation must be a positive integer")
    if issue_number is not None and (type(issue_number) is not int or issue_number <= 0):
        raise ValueError("issue_number must be a positive integer")
    if generation > 1 and issue_number is None:
        raise ValueError("an explicit generation requires an issue_number")
    repository_id = source.repository_id(repo)
    if type(repository_id) is not int or repository_id <= 0:
        raise ValueError("GitHub returned an invalid repository ID")
    active = store.active(repo.installation_id, repository_id)
    candidates = set(source.list_labelled(repo))
    if issue_number is not None:
        candidates.intersection_update({issue_number})
    numbers = sorted(candidates | {claim.snapshot.issue_number for claim in active})
    current = {number: source.get_issue(repo, number) for number in numbers}
    blockers = {
        number: source.has_open_blocker(repo, number)
        for number in sorted(candidates)
        if current[number].state == "open"
        and RUN_LABEL in current[number].labels
        and not current[number].is_pull_request
    }
    for claim in active:
        issue = current[claim.snapshot.issue_number]
        reason = (
            "Issue closed"
            if issue.state != "open"
            else "agent:run label removed"
            if RUN_LABEL not in issue.labels
            else None
        )
        if reason:
            store.cancel(claim.snapshot.run_id, reason)
    claimed = []
    for number in sorted(candidates):
        issue = current[number]
        if (
            issue.number != number
            or issue.state != "open"
            or RUN_LABEL not in issue.labels
            or issue.is_pull_request
            or blockers.get(number, True)
        ):
            continue
        claim = store.claim(_snapshot(repo, repository_id, issue, generation))
        if claim:
            claimed.append(claim)
    return ReconcileResult(
        claimed, store.pending_cancellations(repo.installation_id, repository_id)
    )
