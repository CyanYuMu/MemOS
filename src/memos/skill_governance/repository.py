"""Governance operations inside the existing project-locked graph transaction.

Only immutable evidence, packages and decisions are appended. The qualification
cache is derived in the same transaction and never rewrites a Validation node.
"""

from __future__ import annotations

from memos.skill_drafting.domain import hash_json
from memos.skill_governance import domain as legacy_contract
from memos.skill_management.domain import (
    branches,
    digest,
    identity,
    now,
    public_copy,
    require,
    text,
)


def _contract_for(assessment):
    """Version dispatch preserves the producer identity of historical decisions."""
    require(isinstance(assessment, dict))
    version = assessment.get("schemaVersion")
    require(type(version) is int and version in (1, 2), "unsupported_assessment_version")
    if version == 1:
        return legacy_contract
    from memos.skill_branch_validation import governance

    return governance


class GovernanceMixin:
    def _validation(self, records, state, validation_id):
        value = records.get(("validation", validation_id))
        require(value is not None, "not_found", 404)
        candidate = state["candidate"]
        require(
            value["skillId"] == candidate["skill_id"]
            and value["revision"] == candidate["revision"],
            "not_found",
            404,
        )
        return value

    def _assessment(self, records, state, assessment_id):
        value = records.get(("assessment", assessment_id))
        require(value is not None, "not_found", 404)
        self._validation(records, state, value["validationId"])
        return value

    def _active_adjudications(self, records, state):
        rows = self._items(records, "adjudication", state)
        superseded = {row["supersedes"] for row in rows if row["supersedes"] is not None}
        return [row for row in rows if row["adjudicationId"] not in superseded]

    def _refresh_qualification(self, records, state):
        current = state.get("currentAssessments", {})
        active = self._active_adjudications(records, state)
        usable = [
            row
            for row in active
            if current.get(row["validationId"]) == row["assessmentId"]
            and records[("assessment", row["assessmentId"])]["exportHash"] == state["exportHash"]
        ]
        state["qualification"] = {
            "resolvedValidationIds": sorted(
                row["validationId"] for row in usable if row["contribution"]["resolvedHistory"]
            ),
            "contributions": {
                row["validationId"]: {
                    "adjudicationId": row["adjudicationId"],
                    "branchCoverage": row["contribution"]["branchCoverage"],
                    "boundaryCoverage": row["contribution"]["boundaryCoverage"],
                }
                for row in usable
                if row["contribution"]["qualifiedIndependent"]
            },
            # New observations must not silently erase an established counterexample.
            "counterexamples": sorted(
                row["adjudicationId"] for row in active if row["contribution"]["counterexample"]
            ),
            "seriousViolations": sorted(
                row["adjudicationId"] for row in active if row["contribution"]["seriousViolation"]
            ),
            "activeAdjudicationIds": sorted(row["adjudicationId"] for row in active),
        }

    def _governance_detail(self, records, state):
        validations = {item["validationId"] for item in self._items(records, "validation", state)}
        assessments = [
            value
            for (kind, _), value in records.items()
            if kind == "assessment" and value["validationId"] in validations
        ]
        contributions = state.get("qualification", {}).get("contributions", {})
        coverage = []
        for branch in sorted(branches(state["candidate"])):
            supported = [
                value["adjudicationId"]
                for value in contributions.values()
                if branch in value["branchCoverage"]
            ]
            coverage.append(
                {
                    "branchId": branch,
                    "status": "covered" if supported else "missing",
                    "adjudicationIds": sorted(supported),
                }
            )
        return {
            "assessments": assessments,
            "currentAssessments": state.get("currentAssessments", {}),
            "adjudications": self._items(records, "adjudication", state),
            "coverage": coverage,
            "qualification": state.get("qualification", {}),
        }

    def record_assessment(self, skill_id, revision, *, assessment, actor, operation_id):
        """Host-only, after actual artifact verification; never exposed as an HTTP command."""
        identity(skill_id, revision)
        text(actor, maximum=300)
        request = {
            "action": "record_assessment",
            "skillId": skill_id,
            "revision": revision,
            "assessment": public_copy(assessment),
            "actor": actor,
        }

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            require(isinstance(assessment, dict))
            validation = self._validation(records, state, assessment.get("validationId"))
            value = _contract_for(assessment).validate_assessment(
                state["candidate"], validation, assessment
            )
            if ("assessment", value["assessmentId"]) not in records:
                self._put(records, "assessment", value["assessmentId"], value)
                state.setdefault("currentAssessments", {})[value["validationId"]] = value[
                    "assessmentId"
                ]
                self._refresh_qualification(records, state)
                self._changed(
                    records,
                    state,
                    "assessment_recorded",
                    actor,
                    details={"assessmentId": value["assessmentId"]},
                )
            return {**value, "sequence": state["sequence"]}

        return self._run(perform, mutation=True, op=(operation_id, request))[0]

    def prepare_adjudication(self, skill_id, revision, *, assessment_id, source_watermark, actor):
        digest(assessment_id)
        text(actor, maximum=300)

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            self._check_watermark(state, source_watermark)
            assessment = self._assessment(records, state, assessment_id)
            require(
                state.get("currentAssessments", {}).get(assessment["validationId"])
                == assessment_id,
                "assessment_outdated",
                409,
            )
            package = {
                "kind": "adjudication",
                "projectId": self.project_id,
                "skillId": skill_id,
                "revision": revision,
                "sequence": state["sequence"],
                "contentHash": state["candidate"]["content_hash"],
                "exportHash": state["exportHash"],
                "sourceWatermark": state["sourceWatermark"],
                "assessmentId": assessment_id,
                "assessment": assessment,
                "validation": self._validation(records, state, assessment["validationId"]),
                "activeAdjudicationIds": sorted(
                    row["adjudicationId"] for row in self._active_adjudications(records, state)
                ),
                "actor": actor,
                "preparedAt": now(),
            }
            package["packageId"] = hash_json(package)
            package["packageHash"] = hash_json(package)
            self._put(records, "adjudication_package", package["packageId"], package)
            return package

        return self._run(perform, mutation=True)[0]

    def adjudicate(
        self,
        skill_id,
        revision,
        *,
        package_id,
        package_hash,
        expected_sequence,
        operation_id,
        actor,
        reason,
        source_watermark,
        decision,
        supersedes=None,
    ):
        digest(package_id)
        digest(package_hash)
        text(actor, maximum=300)
        text(reason)
        if supersedes is not None:
            digest(supersedes)
        request = {
            "action": "adjudicate",
            "skillId": skill_id,
            "revision": revision,
            "packageId": package_id,
            "packageHash": package_hash,
            "expectedSequence": expected_sequence,
            "actor": actor,
            "reason": reason,
            "sourceWatermark": source_watermark,
            "decision": public_copy(decision),
            "supersedes": supersedes,
        }

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            self._check_sequence(state, expected_sequence)
            self._check_watermark(state, source_watermark)
            package = records.get(("adjudication_package", package_id))
            require(
                package is not None
                and package["packageHash"] == package_hash
                and package["skillId"] == skill_id
                and package["revision"] == revision
                and package["actor"] == actor
                and package["sequence"] == state["sequence"]
                and package["contentHash"] == state["candidate"]["content_hash"]
                and package["exportHash"] == state["exportHash"]
                and package["sourceWatermark"] == state["sourceWatermark"],
                "review_mismatch",
                409,
            )
            assessment = self._assessment(records, state, package["assessmentId"])
            require(
                state.get("currentAssessments", {}).get(assessment["validationId"])
                == assessment["assessmentId"],
                "assessment_outdated",
                409,
            )
            previous = [
                row
                for row in self._active_adjudications(records, state)
                if row["validationId"] == assessment["validationId"]
            ]
            require(
                (not previous and supersedes is None)
                or (len(previous) == 1 and previous[0]["adjudicationId"] == supersedes),
                "adjudication_conflict",
                409,
            )
            contribution = _contract_for(assessment).adjudicate_assessment(
                state["candidate"], package["validation"], assessment, decision
            )
            value = {
                "skillId": skill_id,
                "revision": revision,
                "validationId": assessment["validationId"],
                "assessmentId": assessment["assessmentId"],
                "packageId": package_id,
                "packageHash": package_hash,
                "sequenceBefore": state["sequence"],
                "actor": actor,
                "reason": reason,
                "decidedAt": now(),
                "supersedes": supersedes,
                "contribution": contribution,
            }
            value["adjudicationId"] = hash_json(value)
            self._put(records, "adjudication", value["adjudicationId"], value)
            self._refresh_qualification(records, state)
            self._changed(
                records,
                state,
                "adjudicated",
                actor,
                reason,
                details={"adjudicationId": value["adjudicationId"]},
            )
            return {
                "adjudicationId": value["adjudicationId"],
                "sequence": state["sequence"],
                "state": state["state"],
                "contribution": contribution,
            }

        return self._run(perform, mutation=True, op=(operation_id, request))[0]
