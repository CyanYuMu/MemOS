"""The feasibility baseline preserves real versioning, retry and hook behavior."""

import sys

from pathlib import Path

import pytest

from memos.skill_release_v2.dependency_probe import run_probe


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="requires the qualified macOS backend"
)


@pytest.fixture(scope="module")
def health(tmp_path_factory):
    result = run_probe(
        Path(__file__).resolve().parents[2],
        tmp_path_factory.mktemp("feedback-health") / "probe",
        timeout=90,
        health=True,
    )
    assert result["passed"] is True, result["child"]["failure"]
    assert result["producerSha256"]
    assert result["child"]["health"]["passed"] is True
    return result["child"]["health"]


def test_complete_health_baseline_preserves_history_and_input(health):
    normal = health["cases"]["ordinary_fallback"]
    assert normal["passed"] is True
    assert normal["graphWriteAttempts"] == 1
    assert normal["fields"]["embedding"] == [0.9, 0.1]
    assert normal["fields"]["version"] == 2
    assert normal["history"][0]["memory"] == "Calibration address A"
    assert normal["oldInputUnchanged"] is True
    assert normal["result"]["id"] == "11111111-1111-4111-8111-111111111111"


def test_separate_vector_follows_graph_then_cleanup(health):
    separate = health["cases"]["separate_vector_fallback"]
    assert separate["passed"] is True
    assert "embedding" not in separate["fields"]
    assert separate["events"] == ["graph_write", "vector_sync", "binding_cleanup"]
    assert separate["vectorPayload"] == {
        "embedding": [0.9, 0.1],
        "memory": "Calibration address B",
    }


def test_original_retry_exhausts_three_attempts_without_followup_side_effects(health):
    failed = health["cases"]["persistent_graph_failure"]
    assert failed["passed"] is True
    assert failed["graphWriteAttempts"] == 3
    assert failed["events"] == ["graph_write", "graph_write", "graph_write"]
    assert failed["exceptionType"] == "RetryError"
    assert failed["causeType"] == "RuntimeError"
    assert failed["oldInputUnchanged"] is True


def test_public_hook_registration_is_respected(health):
    hook = health["cases"]["registered_hook"]
    assert hook["passed"] is True
    assert hook["events"] == ["hook", "graph_write", "binding_cleanup"]
    assert hook["fields"]["key"] == "registered-hook-key"
    assert hook["fields"]["version"] == 2
