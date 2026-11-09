"""Lifecycle mixin for the existing project-locked management repository.

Only host probes append observations. Review decisions never modify a historical
observation, validation, assessment, or release. Dependency paths are computed
from immutable fixed-version declarations in the same transaction.
"""

from __future__ import annotations

import copy

from memos.skill_drafting.domain import hash_json
from memos.skill_lifecycle.execution import TICKET_CATEGORIES, ExecutionMixin
from memos.skill_lifecycle.observers import DETECTOR_VERSION
from memos.skill_management.domain import (
    digest,
    identity,
    now,
    qualified_validations,
    readiness,
    require,
    text,
)


CATEGORIES = {
    "lifecycle_binding",
    "lifecycle_observation",
    "lifecycle_root",
    "lifecycle_decision",
    "lifecycle_package",
}
BAD = {"changed", "unavailable", "invalid"}


def digests(values):
    require(isinstance(values, list) and len(values) <= 200)
    for value in values:
        digest(value)
    require(len(values) == len(set(values)))
    return sorted(values)


CATEGORIES |= TICKET_CATEGORIES


class LifecycleMixin(ExecutionMixin):
    def _closure(self, records, state):
        paths = {}
        visited_paths = 0

        def visit(current, path):
            nonlocal visited_paths
            visited_paths += 1
            require(visited_paths <= 1000, "dependency_path_limit", 409)
            key = identity(current["candidate"]["skill_id"], current["candidate"]["revision"])
            require(key not in path, "dependency_cycle", 409)
            path = [*path, key]
            paths.setdefault(key, []).append(path)
            for target in current["candidate"]["requires"]:
                child = records.get(("state", identity(target["skill_id"], target["revision"])))
                require(child is not None, "dependency_missing", 409)
                visit(child, path)

        visit(state, [])
        return paths

    def _propagate_lifecycle(self, records, origin, actor, action):
        key = identity(origin["candidate"]["skill_id"], origin["candidate"]["revision"])
        for (category, target), state in list(records.items()):
            if category != "state" or target == key:
                continue
            # Only REQUIRES declarations participate; RELATED_TO is informational.
            if key in self._closure(records, state):
                self._execution_ordinal(state)
                state["sequence"] += 1
                state["updatedAt"] = now()
                event = {
                    "skillId": state["candidate"]["skill_id"],
                    "revision": state["candidate"]["revision"],
                    "sequence": state["sequence"],
                    "action": "dependency_changed",
                    "actor": actor,
                    "reason": action,
                    "timestamp": state["updatedAt"],
                    "details": {"origin": key},
                }
                self._put(records, "event", hash_json(event), event)

    def lifecycle_subjects(self, skill_id, revision):
        """Private host-only locators. No HTTP action exposes this method."""

        def read(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            binding = records.get(("lifecycle_binding", state.get("lifecycleBinding")))
            return [] if binding is None else binding["subjects"]

        return self._run(read)[0]

    def lifecycle_closure(self, skill_id, revision):
        def read(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            return [
                {
                    "skillId": records[("state", key)]["candidate"]["skill_id"],
                    "revision": records[("state", key)]["candidate"]["revision"],
                }
                for key in self._closure(records, state)
            ]

        return self._run(read)[0]

    def bind_lifecycle(self, skill_id, revision, *, subjects, actor):
        require(isinstance(subjects, list) and 0 < len(subjects) <= 200)
        ids = set()
        for subject in subjects:
            require(
                isinstance(subject, dict)
                and {"subjectId", "kind", "expectedDigest"} <= subject.keys()
            )
            digest(subject["subjectId"])
            digest(subject["expectedDigest"])
            require(subject["kind"] in {"memory", "tool"} and subject["subjectId"] not in ids)
            ids.add(subject["subjectId"])
        text(actor, maximum=300)
        value = {
            "skillId": skill_id,
            "revision": revision,
            "subjects": sorted(copy.deepcopy(subjects), key=lambda s: s["subjectId"]),
        }
        binding_id = hash_json(value)

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            require(state.get("lifecycleBinding") in {None, binding_id}, "immutable_conflict", 409)
            if state.get("lifecycleBinding") is None:
                self._put(records, "lifecycle_binding", binding_id, value)
                state.update(
                    lifecycleBinding=binding_id,
                    lifecycleObservations={},
                    lifecycleRoots={},
                    lifecycleCleared=[],
                )
                self._changed(records, state, "lifecycle_bound", actor)
            return self._lifecycle_projection(records, nodes, state)

        return self._run(perform, mutation=True)[0]

    def _lifecycle_projection(self, records, nodes, state, *, include_dependencies=True):
        paths = self._closure(records, state)
        own_key = identity(state["candidate"]["skill_id"], state["candidate"]["revision"])
        binding = records.get(("lifecycle_binding", state.get("lifecycleBinding")))
        observations = [
            {
                k: item[k]
                for k in (
                    "observationId",
                    "subjectId",
                    "kind",
                    "expectedDigest",
                    "observedDigest",
                    "status",
                    "previousObservationId",
                    "detectorVersion",
                    "reason",
                    "observedAt",
                )
            }
            for item in self._items(records, "lifecycle_observation", state)
        ]
        decisions = [
            {
                k: item[k]
                for k in ("decisionId", "action", "actor", "reason", "decidedAt", "evidenceIds")
            }
            for item in self._items(records, "lifecycle_decision", state)
        ]
        subjects = []
        for subject in binding["subjects"] if binding else []:
            observation_id = state.get("lifecycleObservations", {}).get(subject["subjectId"])
            observed = records.get(("lifecycle_observation", observation_id), {})
            subjects.append(
                {k: subject[k] for k in ("subjectId", "kind", "expectedDigest")}
                | {
                    "observedDigest": observed.get("observedDigest"),
                    "status": observed.get("status", "unavailable"),
                    "observationId": observation_id,
                }
            )
        roots = []
        for key, root_paths in paths.items():
            origin = records[("state", key)]
            for subject_id, root_id in origin.get("lifecycleRoots", {}).items():
                if root_id in origin.get("lifecycleCleared", []):
                    continue
                root = records[("lifecycle_root", root_id)]
                observation_id = origin["lifecycleObservations"][subject_id]
                observation = records[("lifecycle_observation", observation_id)]
                roots.append(
                    {
                        "rootId": root_id,
                        "originSkillId": origin["candidate"]["skill_id"],
                        "originRevision": origin["candidate"]["revision"],
                        "subjectId": subject_id,
                        "kind": root["kind"],
                        "status": "restored_pending"
                        if observation["status"] in {"healthy", "restored"}
                        else "active",
                        "latestObservationId": observation_id,
                        "paths": root_paths,
                        "reason": observation["reason"],
                    }
                )
        dependencies = []
        if include_dependencies:
            for key in sorted(paths):
                if key == own_key:
                    continue
                dependency = records[("state", key)]
                projected = self._lifecycle_state(records, nodes, dependency)
                gate = readiness(projected, self._items(records, "validation", dependency), [])
                allowed = (
                    dependency["state"] == "published"
                    and bool(dependency.get("releaseId"))
                    and gate["status"] == "PASS"
                )
                dependencies.append(
                    {
                        "skillId": dependency["candidate"]["skill_id"],
                        "revision": dependency["candidate"]["revision"],
                        "releaseId": dependency.get("releaseId"),
                        "state": dependency["state"],
                        "allowed": allowed,
                    }
                )
        reasons = []
        if roots:
            reasons.append("lifecycle_roots_open")
        if binding and any(s["observationId"] is None for s in subjects):
            reasons.append("lifecycle_observation_missing")
        if any(not d["allowed"] for d in dependencies):
            reasons.append("dependency_unavailable")
        current_releases = [
            {k: d[k] for k in ("skillId", "revision", "releaseId")}
            for d in dependencies
            if d["releaseId"] is not None
        ]
        if state.get("releaseId"):
            release = (
                nodes.get(("Release", state["releaseId"]), {})
                .get("payload", {})
                .get("approved_record", {})
            )
            if release.get("dependencyReleases", []) != current_releases:
                reasons.append("dependency_release_changed")
        value = {
            "schemaVersion": 1,
            "enabled": binding is not None,
            "subjects": subjects,
            "roots": sorted(roots, key=lambda r: r["rootId"]),
            "observations": sorted(
                observations, key=lambda o: (o["observedAt"], o["observationId"])
            ),
            "decisions": sorted(decisions, key=lambda d: (d["decidedAt"], d["decisionId"])),
            "dependencies": dependencies,
            "gate": {"status": "BLOCK" if reasons else "PASS", "reasons": reasons},
        }
        # Sequence deliberately participates: even a new review fact invalidates old packages.
        value["watermark"] = hash_json(
            [value, [(key, records[("state", key)]["sequence"]) for key in sorted(paths)]]
        )
        return value

    def _lifecycle_state(self, records, nodes, state):
        projected = copy.deepcopy(state)
        projected["lifecycle"] = self._lifecycle_projection(records, nodes, state)
        return projected

    def _validation_lifecycle_context(self, records, nodes, state):
        observations = []
        healthy = bool(state.get("lifecycleBinding"))
        for key in self._closure(records, state):
            current = records[("state", key)]
            binding = records.get(("lifecycle_binding", current.get("lifecycleBinding")))
            if binding is None:
                healthy = False
                continue
            for subject in binding["subjects"]:
                observed_id = current.get("lifecycleObservations", {}).get(subject["subjectId"])
                observation = records.get(("lifecycle_observation", observed_id))
                if observation is None or observation["status"] not in {"healthy", "restored"}:
                    healthy = False
                if observed_id is not None:
                    observations.append(observed_id)
        return {"observationIds": sorted(observations), "healthy": healthy}

    def _lifecycle_release_binding(self, records, nodes, state):
        projection = self._lifecycle_projection(records, nodes, state)
        if not projection["enabled"] and not state["candidate"]["requires"]:
            return {}
        return {
            "lifecycleWatermark": projection["watermark"],
            "dependencyReleases": [
                {k: d[k] for k in ("skillId", "revision", "releaseId")}
                for d in projection["dependencies"]
                if d["releaseId"] is not None
            ],
        }

    def scan_lifecycle(
        self, skill_id, revision, *, observer, actor, reason, operation_id, expected_sequence=None
    ):
        """Probe outside the graph lock, then commit the entire closure atomically.

        The stable request identity permits an exact retry without re-probing.
        Every captured sequence is checked before the first observation is written.
        """
        text(actor, maximum=300)
        text(reason)
        request = {
            "action": "impact_scan_closure",
            "skillId": skill_id,
            "revision": revision,
            "expectedSequence": expected_sequence,
            "actor": actor,
            "reason": reason,
        }

        def capture(records, nodes, edges):
            previous = records.get(("operation", operation_id))
            if previous is not None:
                require(previous["requestHash"] == hash_json(request), "operation_conflict", 409)
                return {"replay": previous["result"]}
            state = self._state(records, skill_id, revision)
            if expected_sequence is not None:
                self._check_sequence(state, expected_sequence)
            captured = []
            for key in reversed(list(self._closure(records, state))):
                current = records[("state", key)]
                binding = records.get(("lifecycle_binding", current.get("lifecycleBinding")))
                captured.append(
                    {
                        "key": key,
                        "sequence": current["sequence"],
                        "subjects": binding["subjects"] if binding else [],
                    }
                )
            return {"captured": captured}

        prepared = self._run(capture)[0]
        if "replay" in prepared:
            return prepared["replay"]
        captured = prepared["captured"]
        unique_subjects = {
            hash_json(subject): subject for entry in captured for subject in entry["subjects"]
        }
        observed = observer(list(unique_subjects.values())) if unique_subjects else []
        require(
            isinstance(observed, list) and len(observed) == len(unique_subjects),
            "observation_mismatch",
            409,
        )
        by_subject = {}
        for (subject_key, subject), observation in zip(
            unique_subjects.items(), observed, strict=True
        ):
            require(
                isinstance(observation, dict)
                and observation.get("subjectId") == subject["subjectId"],
                "observation_mismatch",
                409,
            )
            by_subject[subject_key] = observation
        observations = {
            entry["key"]: [by_subject[hash_json(subject)] for subject in entry["subjects"]]
            for entry in captured
            if entry["subjects"]
        }

        def perform(records, nodes, edges):
            for entry in captured:
                self._check_sequence(records[("state", entry["key"])], entry["sequence"])
            changed = False
            for entry in captured:
                if entry["subjects"]:
                    result = self._apply_observations(
                        records,
                        nodes,
                        records[("state", entry["key"])],
                        observations[entry["key"]],
                        actor,
                        reason,
                    )
                    changed = changed or result["changed"]
            state = self._state(records, skill_id, revision)
            return {
                "sequence": state["sequence"],
                "changed": changed,
                "lifecycle": self._lifecycle_projection(records, nodes, state),
            }

        return self._run(perform, mutation=True, op=(operation_id, request))[0]

    def record_observations(
        self, skill_id, revision, *, observations, expected_sequence, actor, reason, operation_id
    ):
        text(actor, maximum=300)
        text(reason)
        require(isinstance(observations, list))
        request = {
            "action": "impact_scan",
            "skillId": skill_id,
            "revision": revision,
            "observations": observations,
            "expectedSequence": expected_sequence,
            "actor": actor,
            "reason": reason,
        }

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            self._check_sequence(state, expected_sequence)
            return self._apply_observations(records, nodes, state, observations, actor, reason)

        return self._run(perform, mutation=True, op=(operation_id, request))[0]

    def _apply_observations(self, records, nodes, state, observations, actor, reason):
        binding = records.get(("lifecycle_binding", state.get("lifecycleBinding")))
        require(binding is not None, "lifecycle_not_enabled", 409)
        subjects = {s["subjectId"]: s for s in binding["subjects"]}
        require(
            len(observations) == len(subjects)
            and {o.get("subjectId") for o in observations} == set(subjects)
        )
        changed = False
        for observed in observations:
            require(set(observed) == {"subjectId", "status", "observedDigest", "reason"})
            require(observed["status"] in BAD | {"healthy"})
            text(observed["reason"], maximum=200)
            if observed["observedDigest"] is not None:
                digest(observed["observedDigest"])
            subject = subjects[observed["subjectId"]]
            require(
                (observed["status"] == "healthy")
                == (observed["observedDigest"] == subject["expectedDigest"]),
                "observation_mismatch",
                409,
            )
            previous_id = state["lifecycleObservations"].get(observed["subjectId"])
            previous = records.get(("lifecycle_observation", previous_id), {})
            status = observed["status"]
            if status == "healthy" and previous.get("status") in BAD | {"restored"}:
                status = "restored"
            if all(previous.get(k) == v for k, v in {**observed, "status": status}.items()):
                continue
            value = {
                "skillId": state["candidate"]["skill_id"],
                "revision": state["candidate"]["revision"],
                **observed,
                "kind": subject["kind"],
                "expectedDigest": subject["expectedDigest"],
                "status": status,
                "previousObservationId": previous_id,
                "detectorVersion": DETECTOR_VERSION,
                "observedAt": now(),
            }
            value["observationId"] = hash_json(value)
            self._put(records, "lifecycle_observation", value["observationId"], value)
            state["lifecycleObservations"][subject["subjectId"]] = value["observationId"]
            existing = state["lifecycleRoots"].get(subject["subjectId"])
            if status in BAD and (existing is None or existing in state["lifecycleCleared"]):
                root = {
                    "skillId": state["candidate"]["skill_id"],
                    "revision": state["candidate"]["revision"],
                    "subjectId": subject["subjectId"],
                    "kind": subject["kind"],
                    "firstObservationId": value["observationId"],
                    "openedSequence": max(state.get("lifecycleOrdinal", 0), state["sequence"]) + 1,
                    "priorValidationIds": sorted(
                        v["validationId"] for v in self._items(records, "validation", state)
                    ),
                }
                root_id = hash_json(root)
                self._put(records, "lifecycle_root", root_id, root)
                state["lifecycleRoots"][subject["subjectId"]] = root_id
            changed = True
        if changed:
            self._changed(records, state, "lifecycle_observed", actor, reason)
        return {
            "sequence": state["sequence"],
            "changed": changed,
            "lifecycle": self._lifecycle_projection(records, nodes, state),
        }

    def prepare_impact(self, skill_id, revision, *, actor):
        text(actor, maximum=300)

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            lifecycle = self._lifecycle_projection(records, nodes, state)
            evidence = {v["validationId"] for v in self._items(records, "validation", state)}
            evidence.update(o["observationId"] for o in lifecycle["observations"])
            evidence.update(state.get("currentAssessments", {}).values())
            evidence.update(state.get("qualification", {}).get("activeAdjudicationIds", []))
            evidence.update(f["fact_id"] for f in state.get("lifecycleFacts", []))
            package = {
                "kind": "lifecycle",
                "projectId": self.project_id,
                "skillId": skill_id,
                "revision": revision,
                "sequence": state["sequence"],
                "contentHash": state["candidate"]["content_hash"],
                "exportHash": state["exportHash"],
                "sourceWatermark": state["sourceWatermark"],
                "lifecycleWatermark": lifecycle["watermark"],
                "activeRootIds": sorted(r["rootId"] for r in lifecycle["roots"]),
                "currentChallengeIds": sorted(
                    c["challengeId"] for c in state["challenges"] if c["status"] == "open"
                ),
                "evidenceIds": sorted(evidence),
                "dependencyReleases": [
                    {k: d[k] for k in ("skillId", "revision", "releaseId")}
                    for d in lifecycle["dependencies"]
                    if d["releaseId"] is not None
                ],
                "actor": actor,
                "preparedAt": now(),
            }
            package["packageId"] = hash_json(package)
            package["packageHash"] = hash_json(package)
            self._put(records, "lifecycle_package", package["packageId"], package)
            return package

        return self._run(perform, mutation=True)[0]

    def _check_impact(
        self, records, nodes, state, package_id, package_hash, actor, expected_sequence
    ):
        digest(package_id)
        digest(package_hash)
        self._check_sequence(state, expected_sequence)
        package = records.get(("lifecycle_package", package_id))
        require(
            package is not None
            and package["packageHash"] == package_hash
            and package["actor"] == actor
            and package["skillId"] == state["candidate"]["skill_id"]
            and package["revision"] == state["candidate"]["revision"]
            and package["sequence"] == state["sequence"]
            and package["lifecycleWatermark"]
            == self._lifecycle_projection(records, nodes, state)["watermark"],
            "review_mismatch",
            409,
        )
        return package

    def _lifecycle_decision(self, records, state, action, actor, reason, evidence, **extra):
        value = {
            "skillId": state["candidate"]["skill_id"],
            "revision": state["candidate"]["revision"],
            "action": action,
            "actor": actor,
            "reason": reason,
            "decidedAt": now(),
            "evidenceIds": evidence,
            **extra,
        }
        value["decisionId"] = hash_json(value)
        self._put(records, "lifecycle_decision", value["decisionId"], value)
        self._changed(
            records, state, action, actor, reason, details={"decisionId": value["decisionId"]}
        )

    def _replacement(self, records, nodes, edges, state, replacement_revision):
        digest(replacement_revision)
        c = state["candidate"]
        require(replacement_revision != c["revision"], "replacement_mismatch", 409)
        replacement = self._state(records, c["skill_id"], replacement_revision)
        projected = self._lifecycle_state(records, nodes, replacement)
        require(
            replacement["state"] == "published"
            and replacement.get("releaseId")
            and readiness(projected, self._items(records, "validation", replacement), [])["status"]
            == "PASS",
            "replacement_not_ready",
            409,
        )
        release = nodes[("Release", replacement["releaseId"])]["payload"]["approved_record"]
        require(release["purpose"] == "actual", "replacement_actual_required", 409)
        self._core_edge(
            edges,
            "SUPERSEDES",
            {"kind": "SkillRevision", "key": identity(c["skill_id"], replacement_revision)},
            {"kind": "SkillRevision", "key": identity(c["skill_id"], c["revision"])},
            {"approval_ref": replacement["releaseId"], "scope": "same_skill_fixed_revision"},
        )
        return replacement["releaseId"]

    def review_impact(
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
        decision,
        root_ids,
        validation_ids,
        replacement_revision,
    ):
        text(actor, maximum=300)
        text(reason)
        roots, validations = digests(root_ids), digests(validation_ids)
        require(decision in {"restore", "retain", "supersede"})
        require((replacement_revision is not None) == (decision == "supersede"))
        request = {
            "action": "impact_review",
            "skillId": skill_id,
            "revision": revision,
            "packageId": package_id,
            "packageHash": package_hash,
            "expectedSequence": expected_sequence,
            "actor": actor,
            "reason": reason,
            "decision": decision,
            "rootIds": roots,
            "validationIds": validations,
            "replacementRevision": replacement_revision,
        }

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            package = self._check_impact(
                records, nodes, state, package_id, package_hash, actor, expected_sequence
            )
            require(
                set(roots) <= set(package["activeRootIds"])
                and set(validations) <= set(package["evidenceIds"]),
                "evidence_not_current",
                409,
            )
            extra = {"packageId": package_id, "rootIds": roots}
            if decision == "restore":
                require(state["state"] != "withdrawn", "invalid_transition", 409)
                require(validations, "restoration_validation_required", 422)
                require(state.get("lifecycleBinding") is not None, "lifecycle_not_enabled", 409)
                if state["state"] == "paused":
                    require(
                        not set(validations) & set(state.get("pausedValidationIds", [])),
                        "restoration_validation_stale",
                        422,
                    )
                approved = {
                    v["validationId"]: v
                    for v in qualified_validations(state, self._items(records, "validation", state))
                    if v["purpose"] == "actual"
                    and v["validationId"] in state.get("qualification", {}).get("contributions", {})
                    and v.get("taskResult", {}).get("accepted") is True
                    and v.get("loadVerified") is True
                    and v.get("skillUsage", {}).get("capture") == "complete"
                    and v["exportHash"] == state["exportHash"]
                }
                require(set(validations) <= approved.keys(), "restoration_validation_required", 422)
                lifecycle = self._lifecycle_projection(records, nodes, state)
                own_roots = {
                    r["rootId"]: r
                    for r in lifecycle["roots"]
                    if r["originSkillId"] == skill_id and r["originRevision"] == revision
                }
                require(set(roots) <= own_roots.keys(), "restore_at_origin_required", 409)
                require(
                    all(s["status"] in {"healthy", "restored"} for s in lifecycle["subjects"]),
                    "observation_not_restored",
                    409,
                )
                for root_id in roots:
                    root = records[("lifecycle_root", root_id)]
                    require(
                        own_roots[root_id]["status"] == "restored_pending",
                        "observation_not_restored",
                        409,
                    )
                    require(
                        not set(validations) & set(root["priorValidationIds"]),
                        "restoration_validation_stale",
                        422,
                    )
                if not roots:
                    require(state["state"] == "paused", "invalid_transition", 409)
                    require(
                        not set(validations) & set(state.get("pausedValidationIds", [])),
                        "restoration_validation_stale",
                        422,
                    )
                context = self._validation_lifecycle_context(records, nodes, state)
                require(
                    context["healthy"]
                    and all(
                        state.get("validationLifecycle", {}).get(v) == context for v in validations
                    ),
                    "restoration_validation_context_mismatch",
                    422,
                )
                thresholds = [
                    records[("lifecycle_root", root_id)].get("openedSequence") for root_id in roots
                ]
                if state["state"] == "paused":
                    thresholds.append(state.get("pausedExecutionSequence"))
                require(
                    thresholds and all(type(value) is int for value in thresholds),
                    "restoration_execution_required",
                    422,
                )
                for validation_id in validations:
                    self._require_recovery_execution(
                        records, nodes, state, validation_id, max(thresholds)
                    )
                state["lifecycleCleared"] = sorted(
                    set(state.get("lifecycleCleared", [])) | set(roots)
                )
                if state["state"] == "paused" and not any(
                    c["status"] == "open" for c in state["challenges"]
                ):
                    state["state"] = "published" if state.get("releaseId") else "candidate"
            elif decision == "supersede":
                require(not roots and not validations)
                extra["replacementReleaseId"] = self._replacement(
                    records, nodes, edges, state, replacement_revision
                )
            self._lifecycle_decision(records, state, decision, actor, reason, validations, **extra)
            return {
                "state": state["state"],
                "sequence": state["sequence"],
                "lifecycle": self._lifecycle_projection(records, nodes, state),
            }

        return self._run(perform, mutation=True, op=(operation_id, request))[0]

    def resolve_challenge(
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
        challenge_id,
        decision,
        evidence_ids,
        replacement_revision,
    ):
        text(actor, maximum=300)
        text(reason)
        digest(challenge_id)
        evidence = digests(evidence_ids)
        require(decision in {"addressed", "upheld", "superseded"})
        require((replacement_revision is not None) == (decision == "superseded"))
        require(decision != "addressed" or evidence, "challenge_evidence_required", 422)
        request = {
            "action": "challenge_resolve",
            "skillId": skill_id,
            "revision": revision,
            "packageId": package_id,
            "packageHash": package_hash,
            "expectedSequence": expected_sequence,
            "actor": actor,
            "reason": reason,
            "challengeId": challenge_id,
            "decision": decision,
            "evidenceIds": evidence,
            "replacementRevision": replacement_revision,
        }

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            package = self._check_impact(
                records, nodes, state, package_id, package_hash, actor, expected_sequence
            )
            require(
                challenge_id in package["currentChallengeIds"]
                and set(evidence) <= set(package["evidenceIds"]),
                "evidence_not_current",
                409,
            )
            extra = {"packageId": package_id, "challengeId": challenge_id}
            if decision == "superseded":
                extra["replacementReleaseId"] = self._replacement(
                    records, nodes, edges, state, replacement_revision
                )
            challenge = next(c for c in state["challenges"] if c["challengeId"] == challenge_id)
            if decision != "upheld":
                challenge["status"] = decision
            # Resolving a candidate's challenge never repairs a historical unknown.
            if state["state"] in {"blocked", "needs_evidence"} and not any(
                c["status"] == "open" for c in state["challenges"]
            ):
                state["state"] = "candidate"
            self._lifecycle_decision(
                records, state, "challenge_" + decision, actor, reason, evidence, **extra
            )
            return {
                "state": state["state"],
                "sequence": state["sequence"],
                "lifecycle": self._lifecycle_projection(records, nodes, state),
            }

        return self._run(perform, mutation=True, op=(operation_id, request))[0]
