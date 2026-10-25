"""Owner-pivot depth/provenance contract (2 hops, then stop)."""
import asyncio
from types import SimpleNamespace

import pytest

from app.workers.tasks import pivot_tasks

SEARCH_GITHUB_USER_RUN = pivot_tasks.search_github_user.run


class _Pipeline:
    def __init__(self, redis):
        self._redis = redis
        self._ops = []

    def incr(self, key):
        self._ops.append(("incr", key))
        return self

    def ttl(self, key):
        self._ops.append(("ttl", key))
        return self

    def execute(self):
        out = []
        for op, key in self._ops:
            if op == "incr":
                self._redis.counters[key] = int(self._redis.counters.get(key, 0)) + 1
                out.append(self._redis.counters[key])
            else:
                out.append(self._redis.ttls.get(key, -1))
        return out


class MemoryRedis:
    """Minimal Redis surface used by the pivot gates (no live server)."""

    def __init__(self):
        self.values = {}
        self.counters = {}
        self.ttls = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = str(value)
        return True

    def expire(self, key, seconds):
        self.ttls[key] = int(seconds)
        return True

    def pipeline(self):
        return _Pipeline(self)


async def _no_sleep(_seconds):
    return None


def install_github_results(monkeypatch, results):
    """Give GithubService canned first-call results; later queries return []."""
    calls = []

    class FakeGithub:
        def __init__(self):
            pass

        async def search(self, query):
            calls.append(query)
            return list(results) if len(calls) == 1 else []

    monkeypatch.setattr("app.services.scanners.GithubService", FakeGithub)
    return calls


@pytest.fixture
def pivot_env(monkeypatch):
    redis = MemoryRedis()
    monkeypatch.setattr(pivot_tasks, "redis_client", redis)

    saves = []

    async def fake_save(results, source_name):
        saves.append((results, source_name))
        return len(results)

    monkeypatch.setattr(pivot_tasks, "_save_credentials_async", fake_save)
    monkeypatch.setattr(pivot_tasks, "asyncio", SimpleNamespace(sleep=_no_sleep))
    monkeypatch.setattr(pivot_tasks, "_run_sync", asyncio.run)

    owner_enqueues = []
    bot_enqueues = []
    webhook_enqueues = []
    monkeypatch.setattr(
        pivot_tasks,
        "search_github_user",
        SimpleNamespace(apply_async=lambda **kwargs: owner_enqueues.append(kwargs)),
    )
    monkeypatch.setattr(
        pivot_tasks,
        "search_bot_username",
        SimpleNamespace(apply_async=lambda **kwargs: bot_enqueues.append(kwargs)),
    )
    monkeypatch.setattr(
        pivot_tasks,
        "search_webhook_host",
        SimpleNamespace(apply_async=lambda **kwargs: webhook_enqueues.append(kwargs)),
    )
    return SimpleNamespace(
        redis=redis,
        saves=saves,
        owner_enqueues=owner_enqueues,
        bot_enqueues=bot_enqueues,
        webhook_enqueues=webhook_enqueues,
    )


def test_depth_zero_credential_enqueues_first_owner_hop(pivot_env):
    decision = pivot_tasks.fan_out_pivots(
        {"repo": "Alice/leaky", "source": "github"},
        bot_username="prawn_bot",
        webhook_url=None,
    )

    assert decision == {"owner": True, "bot_username": True, "webhook_host": False}
    assert pivot_env.owner_enqueues == [
        {"args": ["Alice"], "kwargs": {"pivot_depth": 1}, "queue": "validation"}
    ]
    assert pivot_env.bot_enqueues == [{"args": ["prawn_bot"], "queue": "validation"}]
    assert pivot_env.webhook_enqueues == []


def test_first_hop_tags_candidates_and_does_not_re_pivot_same_owner(pivot_env, monkeypatch):
    results = [
        {"token": "111:aaa", "meta": {"repo": "Alice/repo-one", "path": "a.py"}},
        {"token": "222:bbb", "meta": {"repo": "alice/repo-two"}},
    ]
    install_github_results(monkeypatch, results)

    assert SEARCH_GITHUB_USER_RUN("Alice", pivot_depth=1) == "pivoted:Alice:2"

    saved_items, source = pivot_env.saves[0]
    assert source == "pivot_gh_user:Alice"
    assert [item["meta"]["pivot_owner"] for item in saved_items] == ["Alice", "Alice"]
    assert [item["meta"]["pivot_depth"] for item in saved_items] == [1, 1]
    assert saved_items[0]["meta"]["repo"] == "Alice/repo-one"
    assert pivot_env.owner_enqueues == []


def test_depth_one_candidate_enqueues_second_owner_hop(pivot_env):
    decision = pivot_tasks.fan_out_pivots(
        {"repo": "bob/repo", "pivot_owner": "alice", "pivot_depth": 1}
    )

    assert decision["owner"] is True
    assert pivot_env.owner_enqueues == [
        {"args": ["bob"], "kwargs": {"pivot_depth": 2}, "queue": "validation"}
    ]


def test_depth_two_candidate_never_re_enqueues(pivot_env):
    decision = pivot_tasks.fan_out_pivots(
        {"repo": "carol/repo", "pivot_owner": "bob", "pivot_depth": 2}
    )

    assert decision == {"owner": False, "bot_username": False, "webhook_host": False}
    assert pivot_env.owner_enqueues == []


def test_second_hop_tags_depth_two_and_stops(pivot_env, monkeypatch):
    results = [{"token": "333:ccc", "meta": {"repo": "carol/repo"}}]
    install_github_results(monkeypatch, results)

    assert SEARCH_GITHUB_USER_RUN("bob", pivot_depth=2) == "pivoted:bob:1"

    saved_items, _source = pivot_env.saves[0]
    assert saved_items[0]["meta"]["pivot_owner"] == "bob"
    assert saved_items[0]["meta"]["pivot_depth"] == 2
    assert pivot_env.owner_enqueues == []


def test_first_hop_chains_second_hop_for_other_owners_once(pivot_env, monkeypatch):
    results = [
        {"token": "444:ddd", "meta": {"repo": "bob/repo"}},
        {"token": "555:eee", "meta": {"repo": "BOB/other"}},
    ]
    install_github_results(monkeypatch, results)

    SEARCH_GITHUB_USER_RUN("alice", pivot_depth=1)

    assert pivot_env.owner_enqueues == [
        {"args": ["bob"], "kwargs": {"pivot_depth": 2}, "queue": "validation"}
    ]


def test_same_owner_is_not_pivoted_twice_from_one_candidate(pivot_env):
    decision = pivot_tasks.fan_out_pivots(
        {"repo": "Alice/repo", "pivot_owner": "alice", "pivot_depth": 1}
    )

    assert decision["owner"] is False
    assert pivot_env.owner_enqueues == []


def test_cyclic_owner_graph_terminates_at_the_depth_cap(pivot_env, monkeypatch):
    install_github_results(monkeypatch, [{"token": "666:fff", "meta": {"repo": "bob/repo"}}])
    assert SEARCH_GITHUB_USER_RUN("alice", pivot_depth=1) == "pivoted:alice:1"
    assert pivot_env.owner_enqueues == [
        {"args": ["bob"], "kwargs": {"pivot_depth": 2}, "queue": "validation"}
    ]

    install_github_results(monkeypatch, [{"token": "777:ggg", "meta": {"repo": "alice/repo"}}])
    assert SEARCH_GITHUB_USER_RUN("bob", pivot_depth=2) == "pivoted:bob:1"
    assert [call["kwargs"]["pivot_depth"] for call in pivot_env.owner_enqueues] == [2]

    saves_before = len(pivot_env.saves)
    assert SEARCH_GITHUB_USER_RUN("alice", pivot_depth=1) == "skipped:alice"
    assert len(pivot_env.saves) == saves_before
    assert [call["kwargs"]["pivot_depth"] for call in pivot_env.owner_enqueues] == [2]


def test_already_done_gate_suppresses_repeat_owner_pivot(pivot_env, monkeypatch):
    install_github_results(monkeypatch, [{"token": "888:hhh", "meta": {"repo": "alice/repo"}}])

    assert SEARCH_GITHUB_USER_RUN("alice", pivot_depth=1) == "pivoted:alice:1"
    saves_after_first = len(pivot_env.saves)

    assert SEARCH_GITHUB_USER_RUN("alice", pivot_depth=1) == "skipped:alice"
    assert len(pivot_env.saves) == saves_after_first
    assert pivot_env.owner_enqueues == []


def test_budget_exhaustion_suppresses_owner_pivot(pivot_env, monkeypatch):
    pivot_env.redis.counters[pivot_tasks.PIVOT_BUDGET_KEY] = pivot_tasks.PIVOT_BUDGET_MAX
    install_github_results(monkeypatch, [{"token": "999:iii", "meta": {"repo": "alice/repo"}}])

    assert SEARCH_GITHUB_USER_RUN("alice", pivot_depth=1) == "budget-drop:alice"

    assert pivot_env.saves == []
    assert pivot_env.owner_enqueues == []
    assert (
        pivot_env.redis.counters[pivot_tasks.PIVOT_BUDGET_KEY]
        == pivot_tasks.PIVOT_BUDGET_MAX + 1
    )


def test_depth_beyond_cap_is_dropped_before_any_gate(pivot_env):
    assert SEARCH_GITHUB_USER_RUN("alice", pivot_depth=3) == "depth-cap:alice"

    assert pivot_env.redis.values == {}
    assert pivot_env.redis.counters == {}
    assert pivot_env.saves == []
    assert pivot_env.owner_enqueues == []


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({}, 1),
        ({"pivot_depth": None}, 1),
        ({"pivot_depth": "1"}, 2),
        ({"pivot_depth": 1}, 2),
        ({"pivot_depth": 2}, None),
        ({"pivot_depth": 99}, None),
    ],
)
def test_next_owner_pivot_depth_is_default_safe(meta, expected):
    assert pivot_tasks.next_owner_pivot_depth(meta) == expected
