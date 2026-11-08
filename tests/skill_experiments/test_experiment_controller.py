"""Persistent plans must not rerun completed or interrupted model calls."""

from concurrent.futures import Future

import pytest

from memos.skill_experiments import controller as module
from memos.skill_management.domain import ManagementError


class Pool:
    def submit(self, fn, *args):
        self.pending = (fn, args)
        return Future()


@pytest.fixture
def control(tmp_path, monkeypatch):
    manifest = {
        "experimentId": "a" * 64,
        "manifestHash": "a" * 64,
        "projectId": "project",
        "skillId": "skill",
        "revision": "b" * 64,
        "groups": ["none", "raw_memory", "skill"],
        "budget": {},
        "cases": [{"caseId": "holdout-one", "assessmentKind": "repair", "assetDigest": "c" * 64}],
        "cells": [{"cellId": "holdout-one--none", "caseId": "holdout-one", "group": "none"}],
        "sourceWatermark": ["source"],
        "projection": {"exportHash": "d" * 64},
    }
    monkeypatch.setattr(module, "load_experiment", lambda *_: manifest)
    pool = Pool()
    controller = module.ExperimentController(
        {"project": [tmp_path]},
        None,
        {},
        executor=pool,
        authorize=lambda *_: None,
        on_validation=lambda *_: None,
    )
    return controller, pool, tmp_path, manifest


def test_cross_project_and_unknown_id_never_dispatch(control):
    controller, pool, _, manifest = control
    with pytest.raises(ManagementError):
        controller.detail("foreign", manifest["experimentId"])
    with pytest.raises(ManagementError):
        controller.start("project", "missing", "op", "actor")
    assert not hasattr(pool, "pending")


def test_start_idempotency_and_actor_conflict(control):
    controller, _, _, manifest = control
    first = controller.start("project", manifest["experimentId"], "op", "actor")
    assert first["status"] == "running"
    assert controller.start("project", manifest["experimentId"], "op", "actor") == first
    with pytest.raises(ManagementError, match="operation_conflict"):
        controller.start("project", manifest["experimentId"], "op", "other")


def test_recovery_marks_unfinished_unknown_without_resubmission(control):
    controller, _, directory, manifest = control
    controller.start("project", manifest["experimentId"], "op", "actor")
    state = controller._state(directory)
    state["rows"][0]["status"] = "running"
    controller._save(directory, state)
    pool = Pool()
    recovered = module.ExperimentController(
        {"project": [directory]},
        None,
        {},
        executor=pool,
        authorize=lambda *_: None,
        on_validation=lambda *_: None,
    )
    detail = recovered.detail("project", manifest["experimentId"])
    assert detail["status"] == "interrupted"
    assert detail["rows"][0]["status"] == "unknown"
    with pytest.raises(ManagementError, match="experiment_not_startable"):
        recovered.start("project", manifest["experimentId"], "new-op", "actor")
    assert not hasattr(pool, "pending")


def test_worker_failure_retained_and_safe(control, monkeypatch):
    controller, pool, _, manifest = control
    monkeypatch.setattr(
        controller,
        "_execute_cell",
        lambda *_: (_ for _ in ()).throw(ValueError("private token /Users/secret")),
    )
    controller.start("project", manifest["experimentId"], "op", "actor")
    fn, args = pool.pending
    fn(*args)
    detail = controller.detail("project", manifest["experimentId"])
    assert detail["completed"] == 1 and detail["status"] == "completed"
    assert detail["rows"][0]["status"] == "unknown"
    assert "secret" not in str(detail)
    assert detail["publication"] == "not_approved"


def test_recovery_does_not_count_queued_cells_as_executed(control):
    controller, _, directory, manifest = control
    controller.start("project", manifest["experimentId"], "op", "actor")
    recovered = module.ExperimentController(
        {"project": [directory]},
        None,
        {},
        executor=Pool(),
        authorize=lambda *_: None,
        on_validation=lambda *_: None,
    )
    result = recovered.detail("project", manifest["experimentId"])
    assert result["status"] == "interrupted" and result["completed"] == 0
    assert result["rows"][0]["failureReasons"] == ["not_started_due_to_interruption"]


def test_source_failure_prevents_start(control):
    controller, pool, _, manifest = control
    controller.authorize = lambda *_: (_ for _ in ()).throw(ValueError("changed source"))
    with pytest.raises(ManagementError, match="experiment_binding_unavailable"):
        controller.start("project", manifest["experimentId"], "op", "actor")
    assert not hasattr(pool, "pending")


def test_postrun_failure_preserves_actual_cost_and_receipt(control, monkeypatch):
    controller, pool, directory, manifest = control
    row = module._empty(manifest["cells"][0], manifest["cases"][0])
    row.update(
        status="passed",
        elapsedSeconds=37.5,
        reportedCostUsd=0.42,
        receiptVerified=True,
        repairPass=1,
    )
    monkeypatch.setattr(controller, "_execute_cell", lambda *_: (row, directory))
    observations = iter(["stable", "stable", "changed"])
    controller.authorize = lambda *_: next(observations)
    controller.start("project", manifest["experimentId"], "op", "actor")
    fn, args = pool.pending
    fn(*args)
    result = controller.detail("project", manifest["experimentId"])["rows"][0]
    assert result["status"] == "unknown"
    assert result["elapsedSeconds"] == 37.5 and result["reportedCostUsd"] == 0.42
    assert result["receiptVerified"] is True
    assert result["repairPass"] == 0
