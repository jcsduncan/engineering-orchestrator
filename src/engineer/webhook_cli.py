"""Serve signed GitHub issue webhooks and reconcile them off the request path."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fire

from .intake import GitHubIssueSource, IssueClaimStore, RepositoryConfig, reconcile
from .webhooks import (
    MAX_BODY_BYTES,
    DeliveryStore,
    Receipt,
    accept_delivery,
    process_deliveries,
    verify_signature,
)


def make_handler(
    path: Path, repo: RepositoryConfig, repository_id: int, secret: bytes
) -> type[BaseHTTPRequestHandler]:
    """Build an HTTP handler that only verifies and persists deliveries."""

    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def do_POST(self) -> None:
            if self.path != "/webhooks/github":
                self._reply(404, "Not found")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._reply(411, "Content-Length required")
                return
            if length < 0 or length > MAX_BODY_BYTES:
                self._reply(413, "Payload too large")
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self._reply(400, "Incomplete payload")
                return
            if not verify_signature(secret, body, self.headers.get("X-Hub-Signature-256")):
                self._reply(401, "Invalid signature")
                return
            try:
                store = DeliveryStore(path)
                try:
                    receipt = accept_delivery(
                        store, repo, repository_id, secret, dict(self.headers.items()), body
                    )
                finally:
                    store.close()
            except sqlite3.Error:
                receipt = Receipt(503, "Delivery storage unavailable")
            self._reply(receipt.status, receipt.message)

        def _reply(self, status: int, message: str) -> None:
            body = json.dumps({"message": message}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def process_loop(
    stop: threading.Event,
    path: Path,
    source: GitHubIssueSource,
    repo: RepositoryConfig,
    repository_id: int,
    scan_interval: int,
) -> None:
    """Retry queued prompts and perform a full scan for missed events."""
    deliveries = DeliveryStore(path)
    claims = IssueClaimStore(path)
    next_scan = 0.0
    try:
        while not stop.is_set():
            try:
                process_deliveries(deliveries, claims, source, repo, repository_id=repository_id)
                if time.monotonic() >= next_scan:
                    next_scan = time.monotonic() + scan_interval
                    reconcile(source, claims, repo)
            except Exception as exc:
                print(f"Issue reconciliation failed: {exc}", file=sys.stderr)
            stop.wait(1)
    finally:
        claims.close()
        deliveries.close()


def run(
    owner: str,
    repository: str,
    installation_id: int,
    host: str = "127.0.0.1",
    port: int = 8080,
    state_dir: str | None = None,
    scan_interval: int = 300,
) -> None:
    """Listen for signed issue events and reconcile current GitHub state."""
    token = os.getenv("ENGINEER_GITHUB_TOKEN")
    token_file = os.getenv("ENGINEER_GITHUB_TOKEN_FILE")
    secret = os.getenv("ENGINEER_WEBHOOK_SECRET")
    if not token and not token_file:
        raise SystemExit("Set ENGINEER_GITHUB_TOKEN or ENGINEER_GITHUB_TOKEN_FILE")
    if not secret:
        raise SystemExit("Set ENGINEER_WEBHOOK_SECRET to the GitHub App webhook secret")
    if int(scan_interval) <= 0:
        raise ValueError("scan_interval must be positive")
    repo = RepositoryConfig(int(installation_id), str(owner), str(repository))
    path = (
        Path(state_dir).expanduser()
        if state_dir
        else Path.home() / ".local/share/engineering-orchestrator"
    ) / "runs.sqlite3"
    source = (
        GitHubIssueSource(lambda: Path(token_file).read_text().strip())
        if token_file
        else GitHubIssueSource(token)
    )
    repository_id = source.repository_id(repo)
    stop = threading.Event()
    server = ThreadingHTTPServer(
        (str(host), int(port)), make_handler(path, repo, repository_id, secret.encode())
    )
    worker = threading.Thread(
        target=process_loop,
        args=(stop, path, source, repo, repository_id, int(scan_interval)),
        daemon=True,
    )
    worker.start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        worker.join(timeout=5)


def main() -> None:
    fire.Fire(run, name="engineer-webhooks")


if __name__ == "__main__":
    main()
