"""Host-only additive monitoring upgrades; historical bindings never change."""

from copy import deepcopy
from pathlib import Path

from memos.skill_drafting.domain import hash_json
from memos.skill_lifecycle.observers import tool_subject
from memos.skill_management.domain import digest, identity, require, text


def normal_monitor_subject(cli_path, *, paths=None):
    root = Path(__file__).resolve().parents[1]
    if paths is None:
        paths = sorted((root / "skill_management").glob("*.py"))
        paths += sorted((root / "skill_lifecycle").glob("*.py"))
    return tool_subject(cli_path, implementation_paths=paths)


class MaintenanceMixin:
    def lifecycle_binding_id(self, skill_id, revision):
        return self._run(
            lambda records, nodes, edges: self._state(records, skill_id, revision).get(
                "lifecycleBinding"
            )
        )[0]

    def extend_lifecycle(
        self,
        skill_id,
        revision,
        *,
        additions,
        expected_binding,
        expected_sequence,
        actor,
        operation_id,
        reason,
    ):
        identity(skill_id, revision)
        digest(expected_binding)
        text(actor, maximum=300)
        text(reason)
        require(isinstance(additions, list) and 0 < len(additions) <= 20)
        for subject in additions:
            require(isinstance(subject, dict) and subject.get("kind") == "tool")
            digest(subject.get("subjectId"))
            digest(subject.get("expectedDigest"))
        require(len({s["subjectId"] for s in additions}) == len(additions))
        request = {
            "action": "extend_lifecycle",
            "skillId": skill_id,
            "revision": revision,
            "additions": deepcopy(additions),
            "expectedBinding": expected_binding,
            "expectedSequence": expected_sequence,
            "actor": actor,
            "reason": reason,
        }

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            self._check_sequence(state, expected_sequence)
            require(state["state"] == "paused", "maintenance_pause_required", 409)
            require(state.get("lifecycleBinding") == expected_binding, "binding_conflict", 409)
            require(
                not any(
                    j["status"] in {"queued", "running"} for j in self._items(records, "job", state)
                ),
                "busy",
                409,
            )
            require(
                all(
                    ("execution_finish", t["ticketId"]) in records
                    for t in self._items(records, "execution_ticket", state)
                ),
                "busy",
                409,
            )
            original = records[("lifecycle_binding", expected_binding)]
            old_ids = {s["subjectId"] for s in original["subjects"]}
            require(not old_ids & {s["subjectId"] for s in additions}, "immutable_conflict", 409)
            subjects = sorted(
                deepcopy(original["subjects"] + additions), key=lambda s: s["subjectId"]
            )
            require(len(subjects) <= 200)
            value = {
                "skillId": skill_id,
                "revision": revision,
                "subjects": subjects,
                "previousBindingId": expected_binding,
                "policy": "additive-monitoring-1",
            }
            binding = hash_json(value)
            self._put(records, "lifecycle_binding", binding, value)
            state["lifecycleBinding"] = binding
            # Preserve all old observations/roots, but require execution AFTER
            # this upgrade, even if a validation was obtained during the pause.
            state["pausedValidationIds"] = sorted(
                v["validationId"] for v in self._items(records, "validation", state)
            )
            self._changed(
                records,
                state,
                "lifecycle_binding_extended",
                actor,
                reason,
                details={"previousBindingId": expected_binding, "bindingId": binding},
            )
            state["pausedExecutionSequence"] = state["lifecycleOrdinal"]
            return {
                "previousBindingId": expected_binding,
                "bindingId": binding,
                "sequence": state["sequence"],
                "state": state["state"],
            }

        return self._run(perform, mutation=True, op=(operation_id, request))[0]
