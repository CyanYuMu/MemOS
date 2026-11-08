"""One host-owned execution queue, with durable cells and no implicit reruns."""

from __future__ import annotations

import logging
import math
import os
import threading

from pathlib import Path

from memos.skill_drafting.artifacts import load_candidate
from memos.skill_drafting.domain import hash_json
from memos.skill_experiments.manifest import load_experiment, read_json, write_json
from memos.skill_management.domain import ManagementError, now, require
from memos.skill_management.runtime import native_skill


_LOG = logging.getLogger(__name__)
_TERMINAL = {"passed", "failed", "unknown", "infra_invalid"}
_ROW_FIELDS = {
    "cellId",
    "caseId",
    "group",
    "status",
    "assessmentKind",
    "repairPass",
    "boundaryPass",
    "branchSupported",
    "criticalViolation",
    "manualReview",
    "taskAccepted",
    "receiptVerified",
    "loading",
    "capture",
    "elapsedSeconds",
    "reportedCostUsd",
    "toolCalls",
    "failureReasons",
}


def _empty(cell, case):
    return {
        **cell,
        "status": "queued",
        "assessmentKind": case["assessmentKind"],
        "repairPass": None,
        "boundaryPass": None,
        "branchSupported": None,
        "criticalViolation": None,
        "manualReview": "pending",
        "taskAccepted": False,
        "receiptVerified": False,
        "loading": "unknown",
        "capture": "unknown",
        "elapsedSeconds": None,
        "reportedCostUsd": None,
        "toolCalls": None,
        "failureReasons": [],
    }


class ExperimentController:
    def __init__(self, registrations, config, provider, *, executor, authorize, on_validation):
        self.config, self.provider = config, dict(provider)
        self.executor, self.authorize, self.on_validation = executor, authorize, on_validation
        self._lock = threading.RLock()
        self._active = False
        self._stop = threading.Event()
        self.registrations = {}
        for project_id, directories in registrations.items():
            for directory in directories:
                directory = Path(directory).resolve()
                manifest = load_experiment(directory, config)
                require(manifest["projectId"] == project_id, "project_mismatch", 403)
                key = (project_id, manifest["experimentId"])
                require(key not in self.registrations, "duplicate_experiment")
                self.registrations[key] = directory
                if not (directory / "state.json").exists():
                    cases = {c["caseId"]: c for c in manifest["cases"]}
                    self._save(
                        directory,
                        {
                            "experimentId": manifest["experimentId"],
                            "status": "frozen",
                            "rows": [_empty(c, cases[c["caseId"]]) for c in manifest["cells"]],
                            "operations": {},
                            "createdAt": now(),
                        },
                    )
                state = self._state(directory)
                if state["status"] == "running":
                    state["status"] = "interrupted"
                    for row in state["rows"]:
                        if row["status"] == "running":
                            row.update(status="unknown", failureReasons=["service_interrupted"])
                        elif row["status"] == "queued":
                            row["failureReasons"] = ["not_started_due_to_interruption"]
                    self._save(directory, state)

    @property
    def busy(self):
        return self._active

    def close(self):
        self._stop.set()

    def _directory(self, project_id, experiment_id):
        require((project_id, experiment_id) in self.registrations, "experiment_not_found", 404)
        return self.registrations[(project_id, experiment_id)]

    def _save(self, directory, state):
        payload = {k: v for k, v in state.items() if k != "stateHash"}
        payload["stateHash"] = hash_json(payload)
        temporary = directory / "state.next"
        write_json(temporary, payload)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        temporary.replace(directory / "state.json")

    def _state(self, directory):
        state = read_json(directory / "state.json")
        require(
            state.get("stateHash")
            == hash_json({k: v for k, v in state.items() if k != "stateHash"}),
            "experiment_state_changed",
            409,
        )
        return state

    def _item(self, manifest, state):
        return {
            "experimentId": manifest["experimentId"],
            "manifestHash": manifest["manifestHash"],
            "skillId": manifest["skillId"],
            "revision": manifest["revision"],
            "status": state["status"],
            "planned": len(manifest["cells"]),
            "completed": sum(row["status"] in _TERMINAL for row in state["rows"]),
        }

    def list(self, project_id):
        with self._lock:
            items = [
                self._item(load_experiment(d, self.config), self._state(d))
                for (project, _), d in self.registrations.items()
                if project == project_id
            ]
        return {"items": items, "total": len(items)}

    def detail(self, project_id, experiment_id):
        from memos.skill_experiments.scoring import aggregate

        with self._lock:
            directory = self._directory(project_id, experiment_id)
            manifest, state = load_experiment(directory, self.config), self._state(directory)
            rows = [{k: v for k, v in row.items() if k in _ROW_FIELDS} for row in state["rows"]]
            summary = aggregate([r for r in state["rows"] if r["status"] in _TERMINAL])
            summary["completePairs"] = summary["completePairedTasks"]
            for group in summary["groups"]:
                group["costUnknown"] = group.get("unknownCostCount", group.get("costUnknown", 0))
            limitations = [
                "七个独立任务；每任务每组一次，不能证明普遍收益。",
                "边界分流、规则使用与严重违规仍待人工审阅。",
                "实验完成不等于 Skill 可发布；所有失败和未知均保留。",
            ]
            if manifest.get("candidateOrigin") == "source_review_patch_v1":
                limitations.insert(
                    0,
                    "本次 Skill 是模型提炼后经开发者定点修订的候选，不能据此宣称纯自动提炼质量已验证。",
                )
            return {
                **self._item(manifest, state),
                "groups": manifest["groups"],
                "budget": manifest["budget"],
                "rows": rows,
                "summary": summary,
                "publication": "not_approved",
                "limitations": limitations,
            }

    def start(self, project_id, experiment_id, operation_id, actor):
        with self._lock:
            directory = self._directory(project_id, experiment_id)
            manifest, state = load_experiment(directory, self.config), self._state(directory)
            previous = state["operations"].get(operation_id)
            if previous:
                require(previous["actor"] == actor, "operation_conflict", 409)
                return self._item(manifest, state)
            for (registered_project, registered_id), other in self.registrations.items():
                if registered_project == project_id and registered_id != experiment_id:
                    require(
                        operation_id not in self._state(other)["operations"],
                        "operation_conflict",
                        409,
                    )
            require(state["status"] == "frozen", "experiment_not_startable", 409)
            require(not self._active, "busy", 409)
            try:
                self.authorize(project_id, manifest)
            except Exception:
                raise ManagementError("experiment_binding_unavailable", 409) from None
            state["operations"][operation_id] = {"actor": actor, "startedAt": now()}
            state["status"] = "running"
            self._save(directory, state)
            self._active = True
            try:
                self.executor.submit(self._run, project_id, experiment_id, actor)
            except Exception:
                self._active = False
                state["status"] = "interrupted"
                self._save(directory, state)
                raise ManagementError("service_unavailable", 503) from None
            return self._item(manifest, state)

    def _execute_cell(self, directory, manifest, cell):
        from memos.skill_experiments.runner import run_experiment_case
        from memos.skill_experiments.scoring import score_run

        candidate = load_candidate(directory / "input/candidate")["candidate"]
        spec = native_skill(candidate) if cell["group"] == "skill" else None
        raw = (
            (directory / "input/raw-memory.txt").read_text()
            if cell["group"] == "raw_memory"
            else None
        )
        case = next(c for c in manifest["cases"] if c["caseId"] == cell["caseId"])
        attempts = []
        for index in range(1 + manifest["infraRetryLimit"]):
            output = directory / "runs" / cell["cellId"] / f"attempt-{index + 1}"
            report_path = run_experiment_case(
                cell["caseId"],
                output,
                self.config,
                group=cell["group"],
                experiment_id=manifest["experimentId"],
                expected_case_digest=case["assetDigest"],
                skill=spec,
                raw_memory=raw,
                provider_environment=self.provider,
                cancellation_event=self._stop,
            )
            report = read_json(report_path)
            require(
                report.get("experiment_id") == manifest["experimentId"]
                and report.get("group") == cell["group"],
                "experiment_report_mismatch",
                409,
            )
            try:
                score = score_run(
                    report_path, group=cell["group"], expected_case_digest=case["assetDigest"]
                )
            except Exception:
                # A scoring/binding defect cannot make an already billed model
                # call disappear. These metrics remain reported, not verified.
                score = _empty(cell, case)
                score.update(
                    status="unknown",
                    repairPass=None if case["assessmentKind"] == "manual_boundary" else 0,
                    failureReasons=["scoring_unverified"],
                )
                native = report.get("capture", {}).get("native_result") or {}
                for key, value in (
                    ("elapsedSeconds", report.get("execution", {}).get("elapsed_seconds")),
                    ("reportedCostUsd", native.get("total_cost_usd")),
                ):
                    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                        score[key] = value
            attempts.append(
                {
                    "attempt": index + 1,
                    "status": score["status"],
                    "reportSha256": hash_json(report),
                    "elapsedSeconds": score["elapsedSeconds"],
                    "reportedCostUsd": score["reportedCostUsd"],
                }
            )
            write_json(
                output / "score.json",
                {
                    "experimentId": manifest["experimentId"],
                    "cellId": cell["cellId"],
                    "score": score,
                },
            )
            if score["status"] != "infra_invalid":
                break
            _LOG.info(
                "experiment infrastructure failure cell=%s attempt=%s retained",
                cell["cellId"],
                index + 1,
            )
        score["elapsedSeconds"] = (
            sum(a["elapsedSeconds"] for a in attempts)
            if all(a["elapsedSeconds"] is not None for a in attempts)
            else None
        )
        score["reportedCostUsd"] = (
            sum(a["reportedCostUsd"] for a in attempts)
            if all(a["reportedCostUsd"] is not None for a in attempts)
            else None
        )
        return {**cell, **score, "attempts": attempts}, output

    def _run(self, project_id, experiment_id, actor):
        directory = self._directory(project_id, experiment_id)
        try:
            manifest = load_experiment(directory, self.config)
            _LOG.info("experiment started id=%s planned=%s", experiment_id, len(manifest["cells"]))
            for index, cell in enumerate(manifest["cells"]):
                if self._stop.is_set():
                    raise InterruptedError("experiment_stopping")
                with self._lock:
                    state = self._state(directory)
                    if state["rows"][index]["status"] in _TERMINAL:
                        continue
                    state["rows"][index]["status"] = "running"
                    self._save(directory, state)
                _LOG.info(
                    "experiment cell started index=%s/%s case=%s group=%s",
                    index + 1,
                    len(manifest["cells"]),
                    cell["caseId"],
                    cell["group"],
                )
                row = dict(state["rows"][index])
                try:
                    load_experiment(directory, self.config)
                    admission = self.authorize(project_id, manifest)
                    row, output = self._execute_cell(directory, manifest, cell)
                    load_experiment(directory, self.config)
                    require(
                        self.authorize(project_id, manifest) == admission, "admission_changed", 409
                    )
                    if cell["group"] == "skill":
                        self.on_validation(project_id, manifest, row, output)
                except Exception:
                    # Only fixed error categories leave this boundary. A failed
                    # host check cannot erase the attempt's saved artifacts.
                    row.update(
                        status="unknown",
                        repairPass=None if row["assessmentKind"] == "manual_boundary" else 0,
                        failureReasons=[
                            *row.get("failureReasons", []),
                            "execution_or_binding_unverified",
                        ],
                    )
                with self._lock:
                    state = self._state(directory)
                    state["rows"][index] = row
                    self._save(directory, state)
                _LOG.info(
                    "experiment cell finished index=%s status=%s receipt=%s",
                    index + 1,
                    row["status"],
                    row["receiptVerified"],
                )
            with self._lock:
                state = self._state(directory)
                state.update(status="completed", finishedAt=now())
                self._save(directory, state)
            write_json(directory / "report.json", self.detail(project_id, experiment_id))
            _LOG.info(
                "experiment completed id=%s; semantic review pending; not published", experiment_id
            )
        except Exception:
            with self._lock:
                state = self._state(directory)
                state["status"] = "interrupted"
                for row in state["rows"]:
                    if row["status"] == "running":
                        row.update(status="unknown", failureReasons=["experiment_interrupted"])
                    elif row["status"] == "queued":
                        row["failureReasons"] = ["not_started_due_to_interruption"]
                self._save(directory, state)
            _LOG.warning("experiment interrupted id=%s; artifacts retained", experiment_id)
        finally:
            self._active = False
