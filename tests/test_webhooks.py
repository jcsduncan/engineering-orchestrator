import hashlib
import hmac
import http.client
import io
import json
import sqlite3
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer

import pytest

from engineer.intake import GitHubIssueSource, Issue, IssueClaimStore, RepositoryConfig
from engineer.webhook_cli import make_handler, process_loop
from engineer.webhooks import (
    MAX_BODY_BYTES,
    DeliveryStore,
    accept_delivery,
    process_deliveries,
    verify_signature,
)

REPO = RepositoryConfig(17, "owner", "repo")
DELIVERY_ID = "11111111-2222-3333-4444-555555555555"
SECRET = b"test-secret"


class Source:
    def __init__(self):
        self.issue = Issue(
            number=7,
            title="Fix cache",
            body="## Outcome\nFix cache\n\n## Acceptance\n- Add test",
            state="open",
            labels=frozenset({"agent:run"}),
            url="https://github.com/owner/repo/issues/7",
            updated_at="2026-11-06T12:00:00Z",
        )
        self.reads = 0
        self.fail = False

    def repository_id(self, repo):
        assert repo == REPO
        return 99

    def list_labelled(self, repo):
        return [7] if self.issue.state == "open" and "agent:run" in self.issue.labels else []

    def get_issue(self, repo, number):
        self.reads += 1
        if self.fail:
            raise RuntimeError("GitHub unavailable")
        assert repo == REPO and number == 7
        return self.issue

    def has_open_blocker(self, repo, number):
        return False


def signed_delivery(delivery_id=DELIVERY_ID, action="labeled", *, payload=None):
    data = payload or {
        "action": action,
        "installation": {"id": 17},
        "repository": {"id": 99, "full_name": "owner/repo"},
        "issue": {"number": 7, "title": "Old text"},
    }
    body = json.dumps(data).encode()
    signature = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    headers = {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Delivery": delivery_id,
        "X-GitHub-Event": "issues",
    }
    return headers, body


@pytest.fixture
def stores(tmp_path):
    path = tmp_path / "runs.sqlite3"
    deliveries, claims = DeliveryStore(path), IssueClaimStore(path)
    yield deliveries, claims
    claims.close()
    deliveries.close()


def test_signature_matches_github_documented_vector():
    signature = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    assert verify_signature(b"It's a Secret to Everybody", b"Hello, World!", signature)
    assert not verify_signature(b"wrong", b"Hello, World!", signature)
    assert not verify_signature(b"It's a Secret to Everybody", b"Hello, World!", None)
    assert not verify_signature(b"It's a Secret to Everybody", b"Hello, World!", "sha256=é")


def test_signed_delivery_is_persisted_before_any_issue_read(stores):
    deliveries, claims = stores
    source = Source()
    headers, body = signed_delivery()
    receipt = accept_delivery(deliveries, REPO, 99, SECRET, headers, body)
    assert receipt.status == 202 and receipt.message == "Queued"
    assert source.reads == 0
    assert deliveries.get(DELIVERY_ID)["status"] == "pending"
    assert accept_delivery(deliveries, REPO, 99, SECRET, headers, body).message == "Duplicate"

    result = process_deliveries(deliveries, claims, source, REPO, repository_id=99)
    assert result.completed == 1 and result.deferred == 0
    assert source.reads == 1
    assert deliveries.get(DELIVERY_ID)["status"] == "completed"
    claim = claims.pending_claims(17, 99)[0]
    assert claim.snapshot.objective == "Fix cache"
    assert claim.snapshot.title == "Fix cache"
    assert process_deliveries(deliveries, claims, source, REPO, repository_id=99).completed == 0
    assert len(claims.active(17, 99)) == 1


def test_out_of_order_event_uses_current_issue_state(stores):
    deliveries, claims = stores
    source = Source()
    headers, body = signed_delivery(action="unlabeled")
    assert accept_delivery(deliveries, REPO, 99, SECRET, headers, body).status == 202
    assert process_deliveries(deliveries, claims, source, REPO, repository_id=99).completed == 1
    claim = claims.active(17, 99)[0]

    source.issue = replace(source.issue, labels=frozenset())
    older_headers, older_body = signed_delivery(
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", action="opened"
    )
    assert accept_delivery(deliveries, REPO, 99, SECRET, older_headers, older_body).status == 202
    assert process_deliveries(deliveries, claims, source, REPO, repository_id=99).completed == 1
    assert claims.active(17, 99) == []
    assert claims.pending_cancellations(17, 99)[0].snapshot.run_id == claim.snapshot.run_id


def test_bad_signature_or_payload_does_not_persist(stores):
    deliveries, _claims = stores
    headers, body = signed_delivery()
    assert accept_delivery(deliveries, REPO, 99, SECRET, headers, body + b" ").status == 401
    assert (
        accept_delivery(
            deliveries, REPO, 99, SECRET, {**headers, "X-GitHub-Delivery": ""}, body
        ).status
        == 400
    )
    assert (
        accept_delivery(deliveries, REPO, 99, SECRET, headers, b"x" * (MAX_BODY_BYTES + 1)).status
        == 413
    )
    assert deliveries.get(DELIVERY_ID) is None

    invalid = b"not json"
    bad_headers = dict(headers)
    bad_headers["X-Hub-Signature-256"] = (
        "sha256=" + hmac.new(SECRET, invalid, hashlib.sha256).hexdigest()
    )
    assert accept_delivery(deliveries, REPO, 99, SECRET, bad_headers, invalid).status == 400
    assert deliveries.get(DELIVERY_ID) is None


def test_other_repository_and_event_are_ignored(stores):
    deliveries, _claims = stores
    headers, body = signed_delivery()
    assert (
        accept_delivery(deliveries, REPO, 100, SECRET, headers, body).message
        == "Repository ignored"
    )
    assert deliveries.get(DELIVERY_ID) is None
    assert (
        accept_delivery(
            deliveries, REPO, 99, SECRET, {**headers, "X-GitHub-Event": "ping"}, body
        ).message
        == "Event ignored"
    )
    assert deliveries.get(DELIVERY_ID) is None


def test_abandoned_lease_is_retried_without_duplicate_claim(stores):
    deliveries, claims = stores
    source = Source()
    headers, body = signed_delivery()
    accept_delivery(deliveries, REPO, 99, SECRET, headers, body)
    first = deliveries.claim_next(17, 99, lease_seconds=0)
    assert first.attempts == 1
    assert process_deliveries(deliveries, claims, source, REPO, repository_id=99).completed == 1
    assert deliveries.get(DELIVERY_ID)["attempts"] == 2
    assert len(claims.active(17, 99)) == 1


def test_api_failure_defers_delivery_for_retry(stores):
    deliveries, claims = stores
    source = Source()
    headers, body = signed_delivery()
    accept_delivery(deliveries, REPO, 99, SECRET, headers, body)
    source.fail = True
    assert process_deliveries(deliveries, claims, source, REPO, repository_id=99).deferred == 1
    saved = deliveries.get(DELIVERY_ID)
    assert saved["status"] == "pending"
    assert saved["last_error"] == "GitHub unavailable"
    assert claims.active(17, 99) == []
    source.fail = False
    deliveries.db.execute(
        "UPDATE webhook_deliveries SET lease_until = 0 WHERE id = ?", (DELIVERY_ID,)
    )
    deliveries.db.commit()
    assert process_deliveries(deliveries, claims, source, REPO, repository_id=99).completed == 1


def test_processor_leaves_other_repository_deliveries_untouched(stores):
    deliveries, claims = stores
    deliveries.record("other-repository", 17, 100, 7, "labeled")
    result = process_deliveries(deliveries, claims, Source(), REPO, repository_id=99)
    assert result.completed == 0
    assert deliveries.get("other-repository")["attempts"] == 0


def test_github_source_reads_rotated_token_on_each_request(monkeypatch):
    tokens = []
    current = ["first"]

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_open(request, timeout):
        tokens.append(request.get_header("Authorization"))
        return Response(b'{"id": 99}')

    monkeypatch.setattr("engineer.intake.urlopen", fake_open)
    source = GitHubIssueSource(lambda: current[0])
    assert source.repository_id(REPO) == 99
    current[0] = "second"
    assert source.repository_id(REPO) == 99
    assert tokens == ["Bearer first", "Bearer second"]


def test_http_handler_acknowledges_without_fetching_issue(tmp_path):
    path = tmp_path / "runs.sqlite3"
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(path, REPO, 99, SECRET))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        headers, body = signed_delivery()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("POST", "/webhooks/github", body, headers)
        response = connection.getresponse()
        assert response.status == 202
        assert json.loads(response.read()) == {"message": "Queued"}
        connection.close()
        store = DeliveryStore(path)
        try:
            assert store.get(DELIVERY_ID)["status"] == "pending"
        finally:
            store.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_handler_rejects_when_delivery_cannot_be_stored(tmp_path, monkeypatch):
    from engineer import webhook_cli

    def fail_store(_path):
        raise sqlite3.OperationalError("database locked")

    monkeypatch.setattr(webhook_cli, "DeliveryStore", fail_store)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(tmp_path / "runs.sqlite3", REPO, 99, SECRET)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        headers, body = signed_delivery()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("POST", "/webhooks/github", body, headers)
        response = connection.getresponse()
        assert response.status == 503
        assert json.loads(response.read()) == {"message": "Delivery storage unavailable"}
        connection.close()
        assert not (tmp_path / "runs.sqlite3").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_handler_rejects_bad_signature_before_opening_storage(tmp_path):
    path = tmp_path / "runs.sqlite3"
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(path, REPO, 99, SECRET))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        headers, body = signed_delivery()
        headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("POST", "/webhooks/github", body, headers)
        response = connection.getresponse()
        assert response.status == 401
        response.read()
        connection.close()
        assert not path.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_periodic_scan_claims_issue_without_webhook(tmp_path):
    path = tmp_path / "runs.sqlite3"
    source = Source()

    class StopAfterOne:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _seconds):
            self.stopped = True

    process_loop(StopAfterOne(), path, source, REPO, 99, scan_interval=300)
    claims = IssueClaimStore(path)
    try:
        assert claims.pending_claims(17, 99)[0].snapshot.issue_number == 7
    finally:
        claims.close()
