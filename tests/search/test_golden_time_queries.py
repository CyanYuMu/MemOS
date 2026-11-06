"""The query-side golden set must pass in full, and the metrics must not regress."""

import os

import pytest

from tests.memories.textual.golden.query_runner import load_query_cases, metrics, run_world


WORLDS = load_query_cases()


@pytest.fixture(autouse=True)
def _timezone():
    before = os.environ.get("MEMOS_QUERY_TIMEZONE")
    yield
    if before is None:
        os.environ.pop("MEMOS_QUERY_TIMEZONE", None)
    else:
        os.environ["MEMOS_QUERY_TIMEZONE"] = before


@pytest.mark.parametrize("world", WORLDS, ids=[w["id"] for w in WORLDS])
def test_golden_world(world):
    failed = {r["id"]: r["failures"] for r in run_world(world) if not r["ok"]}

    assert not failed, failed


def test_golden_query_metrics():
    results = [r for world in WORLDS for r in run_world(world)]

    summary = metrics(results)

    assert summary["intent_accuracy"] == 1.0
    assert summary["selection_accuracy"] == 1.0
    # queries without time wording, and every rules/fallback answer, cost no model call
    assert summary["llm_calls_per_query"] <= 0.5
    assert summary["llm_free_share"] >= 0.5
