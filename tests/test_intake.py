import io
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from engineer.intake import (
    GitHubIssueSource,
    Issue,
    IssueClaimStore,
    RepositoryConfig,
    reconcile,
)

REPO = RepositoryConfig(17, "owner", "repo")


def make_issue(number=7, *, labels=frozenset({"agent:run"}), state="open", body=None):
    return Issue(
        number=number,
        title="Fix cache",
        body=body
        if body is not None
        else "## Outcome\nFix the cache\n\n## Acceptance\n- Add a regression test\n",
        state=state,
        labels=labels,
        url=f"https://github.com/owner/repo/issues/{number}",
        updated_at="2026-11-06T12:00:00Z",
    )


class Source:
    def __init__(self, issues=None, candidates=None, blocked=None):
        self.issues = issues or {7: make_issue()}
        self.candidates = candidates if candidates is not None else list(self.issues)
        self.blocked = blocked or set()
        self.fetched = []

    def repository_id(self, repo):
        assert repo == REPO
        return 99

    def list_labelled(self, repo):
        assert repo == REPO
        return self.candidates

    def get_issue(self, repo, number):
        assert repo == REPO
        self.fetched.append(number)
        return self.issues[number]

    def has_open_blocker(self, repo, number):
        assert repo == REPO
        return number in self.blocked


@pytest.fixture
def store(tmp_path):
    store = IssueClaimStore(tmp_path / "runs.sqlite3")
    yield store
    store.close()


def test_claim_is_idempotent_and_snapshot_is_immutable(store):
    source = Source()
    first = reconcile(source, store, REPO)
    assert first.cancelled == []
    assert len(first.claimed) == 1
    claim = first.claimed[0]
    assert claim.snapshot.objective == "Fix the cache"
    assert claim.snapshot.acceptance_criteria == "- Add a regression test"
    assert claim.snapshot.body == source.issues[7].body
    assert claim.snapshot.run_id.startswith("issue-")
    source.issues[7] = replace(source.issues[7], title="Changed", body="New instructions")
    assert reconcile(source, store, REPO).claimed == []
    assert store.active(17, 99) == [claim]
    assert source.fetched == [7, 7]


def test_fresh_state_and_open_blockers_gate_claim(store):
    source = Source(
        issues={
            7: make_issue(),
            8: make_issue(8, state="closed"),
            9: make_issue(9, labels=frozenset()),
            10: replace(make_issue(10), is_pull_request=True),
        },
        blocked={7},
    )
    assert reconcile(source, store, REPO).claimed == []
    source.blocked.clear()
    result = reconcile(source, store, REPO)
    assert [claim.snapshot.issue_number for claim in result.claimed] == [7]


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        ({"state": "closed"}, "Issue closed"),
        ({"labels": frozenset()}, "agent:run label removed"),
    ],
)
def test_ineligible_issue_revokes_unfinished_claim(store, changed, reason):
    source = Source()
    original = reconcile(source, store, REPO).claimed[0]
    source.issues[7] = replace(source.issues[7], **changed)
    source.candidates = []
    cancelled = reconcile(source, store, REPO).cancelled
    assert len(cancelled) == 1
    assert cancelled[0].snapshot == original.snapshot
    assert cancelled[0].status == "cancelled"
    assert cancelled[0].reason == reason
    assert reconcile(source, store, REPO).cancelled == cancelled
    store.acknowledge_cancel(original.snapshot.run_id)
    assert reconcile(source, store, REPO).cancelled == []
    assert store.active(17, 99) == []


def test_rerun_requires_new_generation_and_no_overlapping_claim(store):
    source = Source()
    first = reconcile(source, store, REPO).claimed[0]
    assert reconcile(source, store, REPO, generation=2, issue_number=7).claimed == []
    store.complete(first.snapshot.run_id)
    assert reconcile(source, store, REPO).claimed == []
    second = reconcile(source, store, REPO, generation=2, issue_number=7).claimed[0]
    assert second.snapshot.generation == 2
    assert second.snapshot.run_id != first.snapshot.run_id
    source.candidates = []
    source.issues[7] = replace(source.issues[7], state="closed")
    assert [claim.snapshot.run_id for claim in reconcile(source, store, REPO).cancelled] == [
        second.snapshot.run_id
    ]


def test_targeted_reconcile_fetches_one_issue_without_scanning_list(store):
    source = Source(candidates=[])
    result = reconcile(source, store, REPO, issue_number=7)
    assert len(result.claimed) == 1
    assert source.fetched == [7]


def test_cancellation_requires_acknowledgement_after_worker_stops(store):
    source = Source()
    claim = reconcile(source, store, REPO).claimed[0]
    with pytest.raises(ValueError, match="pending cancellation"):
        store.acknowledge_cancel(claim.snapshot.run_id)
    source.candidates = []
    source.issues[7] = replace(source.issues[7], state="closed")
    assert reconcile(source, store, REPO).cancelled[0].snapshot.run_id == claim.snapshot.run_id
    assert reconcile(source, store, REPO).cancelled[0].snapshot.run_id == claim.snapshot.run_id
    store.acknowledge_cancel(claim.snapshot.run_id)
    assert reconcile(source, store, REPO).cancelled == []


def test_two_store_connections_cannot_duplicate_a_claim(tmp_path):
    path = tmp_path / "runs.sqlite3"
    first, second = IssueClaimStore(path), IssueClaimStore(path)
    try:
        source = Source()
        claimed = reconcile(source, first, REPO).claimed
        assert len(claimed) == 1
        assert reconcile(source, second, REPO).claimed == []
        assert second.active(17, 99) == claimed
    finally:
        first.close()
        second.close()


def test_simultaneous_pollers_claim_once(tmp_path):
    path = tmp_path / "runs.sqlite3"
    barrier = Barrier(2)

    def poll():
        store = IssueClaimStore(path)
        try:
            barrier.wait()
            return reconcile(Source(), store, REPO).claimed
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(poll) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(len(claims) for claims in results) == [0, 1]


def test_api_failure_does_not_mutate_claims(store):
    source = Source()
    claimed = reconcile(source, store, REPO).claimed
    source.candidates = []

    def fail(_repo, _number):
        raise RuntimeError("GitHub unavailable")

    source.get_issue = fail
    with pytest.raises(RuntimeError, match="GitHub unavailable"):
        reconcile(source, store, REPO)
    assert store.active(17, 99) == claimed


def test_issue_text_is_stored_as_data_without_changing_claim_permissions(store):
    body = "## Outcome\n--worker 'rm -rf /'\n\n## Acceptance\n- --check='false'"
    source = Source(issues={7: make_issue(body=body)})
    claim = reconcile(source, store, REPO).claimed[0]
    assert claim.snapshot.objective == "--worker 'rm -rf /'"
    assert claim.snapshot.acceptance_criteria == "- --check='false'"
    assert claim.snapshot.body == body


def test_invalid_generation_rejected_before_api_call(store):
    source = Source()
    with pytest.raises(ValueError, match="generation"):
        reconcile(source, store, REPO, generation=0)
    assert source.fetched == []
    with pytest.raises(ValueError, match="issue_number"):
        reconcile(source, store, REPO, generation=2)
    with pytest.raises(ValueError, match="issue_number"):
        reconcile(source, store, REPO, issue_number=0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"installation_id": 0, "owner": "owner", "name": "repo"},
        {"installation_id": 1, "owner": "bad/owner", "name": "repo"},
        {"installation_id": 1, "owner": "owner", "name": ".."},
    ],
)
def test_repository_config_rejects_invalid_identity(kwargs):
    with pytest.raises(ValueError):
        RepositoryConfig(**kwargs)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_github_source_reads_current_issue_and_dependencies(monkeypatch):
    requested = []

    def fake_open(request, timeout):
        requested.append((request.full_url, request.get_header("Authorization"), timeout))
        if request.full_url.endswith("/repos/owner/repo"):
            data = {"id": 99}
        elif "/dependencies/blocked_by" in request.full_url:
            data = [{"state": "closed"}, {"state": "open"}]
        elif "/issues/7" in request.full_url:
            data = {
                "number": 7,
                "title": "Fix cache",
                "body": "task",
                "state": "open",
                "labels": [{"name": "agent:run"}],
                "html_url": "https://github.com/owner/repo/issues/7",
                "updated_at": "2026-11-06T12:00:00Z",
            }
        else:
            data = [{"number": 7}, {"number": 8, "pull_request": {"url": "pr"}}]
        return Response(json.dumps(data).encode())

    monkeypatch.setattr("engineer.intake.urlopen", fake_open)
    source = GitHubIssueSource("secret", timeout=8)
    assert source.repository_id(REPO) == 99
    assert source.list_labelled(REPO) == [7]
    assert source.get_issue(REPO, 7).labels == frozenset({"agent:run"})
    assert source.has_open_blocker(REPO, 7)
    assert all(auth == "Bearer secret" and timeout == 8 for _, auth, timeout in requested)
    assert requested[1][0].startswith("https://api.github.com/repos/owner/repo/issues?state=open")


def test_github_source_paginates_candidates_and_blockers(monkeypatch):
    calls = []

    def fake_open(request, timeout):
        calls.append(request.full_url)
        if "/dependencies/blocked_by" in request.full_url:
            data = [{"state": "closed"}] * 100 if request.full_url.endswith("page=1") else []
        else:
            data = (
                [{"number": number} for number in range(1, 101)]
                if request.full_url.endswith("page=1")
                else [{"number": 101}]
            )
        return Response(json.dumps(data).encode())

    monkeypatch.setattr("engineer.intake.urlopen", fake_open)
    source = GitHubIssueSource("secret")
    assert len(source.list_labelled(REPO)) == 101
    assert not source.has_open_blocker(REPO, 7)
    assert len(calls) == 4
