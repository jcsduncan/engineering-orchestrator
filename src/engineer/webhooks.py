"""Verify GitHub webhooks and persist their reconciliation prompts."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .intake import IssueClaimStore, IssueSource, RepositoryConfig, reconcile

MAX_BODY_BYTES = 1024 * 1024
_DELIVERY_ID = re.compile(r"[A-Za-z0-9-]{1,128}\Z")


@dataclass(frozen=True)
class Delivery:
    id: str
    installation_id: int
    repository_id: int
    issue_number: int
    action: str
    attempts: int


@dataclass(frozen=True)
class Receipt:
    status: int
    message: str


@dataclass(frozen=True)
class ProcessResult:
    completed: int
    deferred: int


class DeliveryStore:
    """Keep webhook delivery IDs and retry leases in SQLite."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    id TEXT PRIMARY KEY,
                    installation_id INTEGER NOT NULL,
                    repository_id INTEGER NOT NULL,
                    issue_number INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'completed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_until INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    received_at INTEGER NOT NULL,
                    completed_at INTEGER
                )"""
            )
            self.db.execute(
                """CREATE INDEX IF NOT EXISTS webhook_deliveries_pending
                ON webhook_deliveries (status, lease_until, received_at)"""
            )

    def close(self) -> None:
        self.db.close()

    def record(
        self,
        delivery_id: str,
        installation_id: int,
        repository_id: int,
        issue_number: int,
        action: str,
    ) -> bool:
        """Persist a verified delivery once before acknowledging its HTTP request."""
        with self.db:
            cursor = self.db.execute(
                """INSERT INTO webhook_deliveries
                (id, installation_id, repository_id, issue_number, action, status, received_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(id) DO NOTHING""",
                (
                    delivery_id,
                    installation_id,
                    repository_id,
                    issue_number,
                    action,
                    int(time.time()),
                ),
            )
        return cursor.rowcount == 1

    def claim_next(
        self, installation_id: int, repository_id: int, *, lease_seconds: int = 60
    ) -> Delivery | None:
        """Lease the oldest pending prompt, including one abandoned after a crash."""
        now = int(time.time())
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                """SELECT * FROM webhook_deliveries WHERE installation_id = ?
                AND repository_id = ? AND status = 'pending' AND lease_until <= ?
                ORDER BY received_at, id LIMIT 1""",
                (installation_id, repository_id, now),
            ).fetchone()
            if row is None:
                return None
            self.db.execute(
                """UPDATE webhook_deliveries SET attempts = attempts + 1, lease_until = ?
                WHERE id = ?""",
                (now + lease_seconds, row["id"]),
            )
        return Delivery(
            row["id"],
            row["installation_id"],
            row["repository_id"],
            row["issue_number"],
            row["action"],
            row["attempts"] + 1,
        )

    def complete(self, delivery_id: str) -> None:
        with self.db:
            cursor = self.db.execute(
                """UPDATE webhook_deliveries SET status = 'completed', lease_until = 0,
                last_error = NULL, completed_at = ? WHERE id = ? AND status = 'pending'""",
                (int(time.time()), delivery_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Delivery is not pending")

    def defer(self, delivery_id: str, error: str, *, delay_seconds: int = 30) -> None:
        """Retain a failed prompt for retry after a bounded delay."""
        with self.db:
            cursor = self.db.execute(
                """UPDATE webhook_deliveries SET lease_until = ?, last_error = ?
                WHERE id = ? AND status = 'pending'""",
                (int(time.time()) + delay_seconds, error[-500:], delivery_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Delivery is not pending")

    def get(self, delivery_id: str) -> dict | None:
        """Inspect persisted delivery state without exposing credentials or payloads."""
        row = self.db.execute(
            "SELECT * FROM webhook_deliveries WHERE id = ?", (delivery_id,)
        ).fetchone()
        return dict(row) if row is not None else None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return next(
        (value for key, value in headers.items() if key.casefold() == name.casefold()), None
    )


def verify_signature(secret: bytes, body: bytes, signature: str | None) -> bool:
    """Check GitHub's HMAC-SHA256 over the original request bytes."""
    if not secret or not signature:
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def accept_delivery(
    store: DeliveryStore,
    repo: RepositoryConfig,
    repository_id: int,
    secret: bytes,
    headers: Mapping[str, str],
    body: bytes,
) -> Receipt:
    """Verify and persist an issue event without fetching GitHub or running a model."""
    if len(body) > MAX_BODY_BYTES:
        return Receipt(413, "Payload too large")
    if not verify_signature(secret, body, _header(headers, "X-Hub-Signature-256")):
        return Receipt(401, "Invalid signature")
    delivery_id = _header(headers, "X-GitHub-Delivery")
    if not delivery_id or not _DELIVERY_ID.fullmatch(delivery_id):
        return Receipt(400, "Invalid delivery ID")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return Receipt(400, "Invalid JSON")
    if not isinstance(payload, dict):
        return Receipt(400, "Invalid payload")
    if _header(headers, "X-GitHub-Event") != "issues":
        return Receipt(202, "Event ignored")
    installation = payload.get("installation") or {}
    repository = payload.get("repository") or {}
    issue = payload.get("issue") or {}
    if not all(isinstance(item, dict) for item in (installation, repository, issue)):
        return Receipt(400, "Invalid payload")
    if (
        installation.get("id") != repo.installation_id
        or repository.get("id") != repository_id
        or str(repository.get("full_name", "")).casefold() != f"{repo.owner}/{repo.name}".casefold()
    ):
        return Receipt(202, "Repository ignored")
    number = issue.get("number")
    if type(number) is not int or number <= 0:
        return Receipt(400, "Invalid issue number")
    action = payload.get("action")
    if not isinstance(action, str):
        return Receipt(400, "Invalid action")
    created = store.record(delivery_id, repo.installation_id, repository_id, number, action)
    return Receipt(202, "Queued" if created else "Duplicate")


def process_deliveries(
    deliveries: DeliveryStore,
    claims: IssueClaimStore,
    source: IssueSource,
    repo: RepositoryConfig,
    *,
    repository_id: int,
    limit: int = 100,
) -> ProcessResult:
    """Fetch current issue state through the same claim operation as polling."""
    completed = deferred = 0
    for _ in range(limit):
        delivery = deliveries.claim_next(repo.installation_id, repository_id)
        if delivery is None:
            break
        try:
            reconcile(source, claims, repo, issue_number=delivery.issue_number)
        except Exception as exc:
            deliveries.defer(delivery.id, str(exc))
            deferred += 1
        else:
            deliveries.complete(delivery.id)
            completed += 1
    return ProcessResult(completed, deferred)
