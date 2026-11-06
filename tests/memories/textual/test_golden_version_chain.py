"""The golden set must pass in full, and the metrics must not regress."""

import pytest

from tests.memories.textual.golden.runner import load_cases, metrics, run_case


CASES = load_cases()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_case(case):
    result = run_case(case)

    assert result["ok"], result["failures"]


def test_golden_metrics():
    results = [run_case(c) for c in CASES]

    summary = metrics(results)

    assert summary["current_accuracy"] == 1.0
    assert summary["past_recall"] == 1.0
    assert summary["event_retention"] == 1.0
    # one detector call per candidate pair at most, and a supersede never spends a fusion call
    assert summary["llm_calls_per_add"] <= 1.0
