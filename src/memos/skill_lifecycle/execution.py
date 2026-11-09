"""Host-issued execution identities bind freshness to graph order and native bytes.

The lifecycle ordinal is independent of management.sequence: issuing a ticket
must not invalidate the normal-use admission it records. Only actual governance
or observation changes advance the public admission sequence.
"""

from __future__ import annotations

import secrets
import uuid

from memos.skill_drafting.domain import hash_json
from memos.skill_management.domain import digest, identity, now, require, text


TICKET_CATEGORIES = {"execution_ticket", "execution_finish"}
_TICKET_FIELDS = (
    "ticketId",
    "runId",
    "attemptId",
    "startedSequence",
    "observationIds",
    "contentHash",
    "exportHash",
    "batchId",
    "unitId",
    "caseId",
    "mode",
    "group",
)


class ExecutionMixin:
    @staticmethod
    def _execution_ordinal(state):
        state["lifecycleOrdinal"] = max(state.get("lifecycleOrdinal", 0), state["sequence"]) + 1
        return state["lifecycleOrdinal"]

    def begin_execution(
        self,
        skill_id,
        revision,
        *,
        batch_id,
        unit_id,
        case_id,
        group,
        mode,
        content_hash,
        export_hash,
        actor,
        operation_id,
    ):
        identity(skill_id, revision)
        for value in (batch_id, content_hash, export_hash):
            digest(value)
        for value in (unit_id, case_id, actor):
            text(value, maximum=300)
        require(
            group in {"none", "skill"}
            and mode in {"independent_validation", "candidate_evaluation", "normal"}
        )
        require(mode == "independent_validation" or group == "skill")
        request = {
            "action": "execution_begin",
            "skillId": skill_id,
            "revision": revision,
            "batchId": batch_id,
            "unitId": unit_id,
            "caseId": case_id,
            "group": group,
            "mode": mode,
            "contentHash": content_hash,
            "exportHash": export_hash,
            "actor": actor,
        }

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            require(
                state["candidate"]["content_hash"] == content_hash
                and state["exportHash"] == export_hash,
                "artifact_mismatch",
                409,
            )
            require(
                not any(
                    row["batchId"] == batch_id and row["unitId"] == unit_id
                    for row in self._items(records, "execution_ticket", state)
                ),
                "execution_unit_already_started",
                409,
            )
            context = self._validation_lifecycle_context(records, nodes, state)
            require(context["healthy"], "execution_observation_unavailable", 409)
            if mode == "normal":
                require(
                    self._admission(records, nodes, state, case_id, False)["allowed"],
                    "release_unavailable",
                    403,
                )
            # Pending restored roots deliberately do not block independent runs:
            # those runs supply the new evidence required to resolve the roots.
            ticket = {
                **{k: v for k, v in request.items() if k != "action"},
                "runId": secrets.token_hex(32),
                "attemptId": secrets.token_hex(16),
                "startedSequence": self._execution_ordinal(state),
                "observationIds": context["observationIds"],
                "startedAt": now(),
            }
            ticket["ticketId"] = hash_json(ticket)
            self._put(records, "execution_ticket", ticket["ticketId"], ticket)
            return {k: ticket[k] for k in _TICKET_FIELDS}

        return self._run(perform, mutation=True, op=(operation_id, request))[0]

    def finish_execution(
        self,
        ticket_id,
        *,
        report_sha256,
        events_sha256,
        initial_receipt_sha256,
        receipt_sha256,
        actor,
        operation_id,
    ):
        for value in (
            ticket_id,
            report_sha256,
            events_sha256,
            initial_receipt_sha256,
            receipt_sha256,
        ):
            digest(value)
        text(actor, maximum=300)
        request = {
            "action": "execution_finish",
            "ticketId": ticket_id,
            "reportSha256": report_sha256,
            "eventsSha256": events_sha256,
            "initialReceiptSha256": initial_receipt_sha256,
            "receiptSha256": receipt_sha256,
            "actor": actor,
        }

        def perform(records, nodes, edges):
            ticket = records.get(("execution_ticket", ticket_id))
            require(
                ticket is not None and ticket["actor"] == actor, "execution_ticket_missing", 409
            )
            require(
                ("execution_finish", ticket_id) not in records, "execution_already_finished", 409
            )
            state = self._state(records, ticket["skillId"], ticket["revision"])
            context = self._validation_lifecycle_context(records, nodes, state)
            healthy = (
                context["healthy"]
                and context["observationIds"] == ticket["observationIds"]
                and state["candidate"]["content_hash"] == ticket["contentHash"]
                and state["exportHash"] == ticket["exportHash"]
            )
            if ticket["mode"] == "normal":
                healthy = (
                    healthy
                    and self._admission(records, nodes, state, ticket["caseId"], False)["allowed"]
                )
            completed = {
                **{k: v for k, v in request.items() if k != "action"},
                "skillId": ticket["skillId"],
                "revision": ticket["revision"],
                "status": "completed" if healthy else "invalid",
                "finishedSequence": self._execution_ordinal(state),
                "observationIds": context["observationIds"],
                "finishedAt": now(),
            }
            completed["completionId"] = hash_json(completed)
            self._put(records, "execution_finish", ticket_id, completed)
            return completed

        return self._run(perform, mutation=True, op=(operation_id, request))[0]

    def _require_recovery_execution(self, records, nodes, state, validation_id, after_sequence):
        assessment_id = state.get("currentAssessments", {}).get(validation_id)
        assessment = records.get(("assessment", assessment_id), {})
        require(assessment.get("schemaVersion") == 6, "restoration_execution_required", 422)
        matches = [
            row
            for row in self._items(records, "execution_ticket", state)
            if row["runId"] == assessment.get("runId")
            and row["attemptId"] == assessment.get("attemptId")
            and row["batchId"] == assessment.get("batchId")
        ]
        require(len(matches) == 1, "restoration_execution_required", 422)
        ticket = matches[0]
        completed = records.get(("execution_finish", ticket["ticketId"]))
        require(
            completed is not None and completed["status"] == "completed",
            "restoration_execution_incomplete",
            422,
        )
        require(
            type(after_sequence) is int and ticket["startedSequence"] > after_sequence,
            "restoration_execution_stale",
            422,
        )
        require(
            ticket["mode"] == "independent_validation" and ticket["group"] == "skill",
            "restoration_execution_mode",
            422,
        )
        require(
            ticket["unitId"]
            == assessment.get("executionQualification", {}).get("trial", {}).get("unit"),
            "restoration_execution_mismatch",
            422,
        )
        validation = records[("validation", validation_id)]
        require(
            all(
                ticket[k] == assessment.get(k) == validation.get(k)
                for k in ("runId", "attemptId", "caseId", "contentHash", "exportHash")
            ),
            "restoration_execution_mismatch",
            422,
        )
        require(
            completed["reportSha256"] == assessment.get("reportSha256")
            and completed["eventsSha256"] == assessment.get("eventsSha256")
            and completed["initialReceiptSha256"]
            == assessment.get("initialVerification", {}).get("receiptSha256")
            and completed["receiptSha256"]
            == assessment.get("verification", {}).get("receiptSha256")
            == validation.get("receipt", {}).get("sha256"),
            "restoration_execution_mismatch",
            422,
        )
        context = self._validation_lifecycle_context(records, nodes, state)
        require(
            context["healthy"]
            and context["observationIds"]
            == ticket["observationIds"]
            == completed["observationIds"],
            "restoration_execution_observation_changed",
            422,
        )


class ExecutionGuard:
    def __init__(self, repository, observer, candidate, actor):
        self.repository, self.observer, self.candidate, self.actor = (
            repository,
            observer,
            candidate,
            actor,
        )

    def _scan(self):
        self.repository.scan_lifecycle(
            self.candidate["skill_id"],
            self.candidate["revision"],
            observer=self.observer,
            actor=self.actor,
            reason="Actual source and tool observations at execution boundary",
            operation_id="execution-scan-" + uuid.uuid4().hex,
        )

    def begin(self, metadata):
        require(
            isinstance(metadata, dict)
            and set(metadata)
            == {"batchId", "unitId", "caseId", "group", "mode", "contentHash", "exportHash"}
        )
        self._scan()
        return self.repository.begin_execution(
            self.candidate["skill_id"],
            self.candidate["revision"],
            batch_id=metadata["batchId"],
            unit_id=metadata["unitId"],
            case_id=metadata["caseId"],
            group=metadata["group"],
            mode=metadata["mode"],
            content_hash=metadata["contentHash"],
            export_hash=metadata["exportHash"],
            actor=self.actor,
            operation_id="execution-begin-" + uuid.uuid4().hex,
        )

    def finish(self, ticket, artifacts):
        require(
            isinstance(artifacts, dict)
            and set(artifacts)
            == {"reportSha256", "eventsSha256", "initialReceiptSha256", "receiptSha256"}
        )
        self._scan()
        return self.repository.finish_execution(
            ticket["ticketId"],
            report_sha256=artifacts["reportSha256"],
            events_sha256=artifacts["eventsSha256"],
            initial_receipt_sha256=artifacts["initialReceiptSha256"],
            receipt_sha256=artifacts["receiptSha256"],
            actor=self.actor,
            operation_id="execution-finish-" + uuid.uuid4().hex,
        )


def make_execution_guard(repository, observer, candidate, actor="host_execution"):
    require(callable(observer), "lifecycle_not_enabled", 409)
    return ExecutionGuard(repository, observer, candidate, actor)
