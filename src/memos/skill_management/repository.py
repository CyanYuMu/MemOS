"""Project-locked management transactions over the independent Skill Graph.

Immutable P2 documents remain unchanged. Mutable serving state and append-only
operations/reviews/jobs live in a separate internal record label; approvals and
validations are also projected into the nine public graph node types.
"""

from __future__ import annotations

import copy
import json

from pathlib import Path

from memos.skill_drafting.domain import hash_json
from memos.skill_drafting.graph import SkillGraphRepository, edge_key, node_key
from memos.skill_governance.repository import GovernanceMixin
from memos.skill_lifecycle.repository import CATEGORIES as LIFECYCLE_CATEGORIES
from memos.skill_lifecycle.repository import LifecycleMixin
from memos.skill_management.domain import (
    ManagementError,
    digest,
    identity,
    now,
    operation,
    public_copy,
    qualified_validations,
    readiness,
    require,
    summary,
    text,
    validate_registration,
    validate_report,
)


_CATEGORIES = {
    "state",
    "validation",
    "job",
    "package",
    "operation",
    "event",
    "assessment",
    "adjudication_package",
    "adjudication",
}


_CATEGORIES |= LIFECYCLE_CATEGORIES


class ManagementRepository(LifecycleMixin, GovernanceMixin, SkillGraphRepository):
    """The trusted service owns these methods, never model-supplied graph writes."""

    max_management_records = 10000

    def initialize(self):
        result = super().initialize()
        if not getattr(self, "_management_initialized", False):
            try:
                with self._driver.session(database=self.database) as session:
                    self._database_guard(session)
                    session.run(
                        "CREATE CONSTRAINT memos_skill_management_uid IF NOT EXISTS FOR (n:SkillManagementRecord) REQUIRE n.uid IS UNIQUE"
                    ).consume()
                self._management_initialized = True
            except Exception:
                raise ManagementError("storage_unavailable", 503) from None
        return result

    def _node(self, node):
        payload = node.get("payload", {}) if isinstance(node, dict) else {}
        if payload.get("management_schema") != 1:
            return super()._node(node)
        kind = node.get("kind")
        require(kind in {"Validation", "Release", "CaseRevision"}, "invalid_graph_record")
        surrogate = copy.deepcopy(node)
        if kind == "Validation":
            report = payload.get("report")
            require(
                isinstance(report, dict)
                and report.get("validationId") == payload.get("validation_id"),
                "invalid_graph_record",
            )
            surrogate["payload"].update(kind="structural", semantic_review="pending")
        elif kind == "Release":
            approved = payload.get("approved_record")
            require(
                isinstance(approved, dict)
                and approved.get("releaseId") == payload.get("release_id")
                and payload.get("authorized") is True,
                "invalid_graph_record",
            )
            require(
                approved["releaseId"]
                == hash_json({k: v for k, v in approved.items() if k != "releaseId"}),
                "invalid_graph_record",
            )
            surrogate["payload"].update(purpose="management_test", authorized=False)
        else:
            require(payload.get("split") in {"dev", "holdout"}, "invalid_graph_record")
            surrogate["payload"]["purpose"] = "management_test"
        super()._node(surrogate)
        return {"kind": kind, "key": node["key"], "payload": public_copy(payload)}

    def store_bundle(self, nodes, edges):
        require(isinstance(nodes, list) and isinstance(edges, list), "invalid_input")
        require(
            not any(isinstance(edge, dict) and edge.get("kind") == "SUPERSEDES" for edge in edges),
            "authority_required",
            403,
        )
        require(
            not any(
                isinstance(node, dict)
                and isinstance(node.get("payload"), dict)
                and (
                    node["payload"].get("management_schema") == 1
                    or (node.get("kind") == "Release" and node["payload"].get("authorized") is True)
                )
                for node in nodes
            ),
            "authority_required",
            403,
        )
        return super().store_bundle(nodes, edges)

    def _semantics(self, nodes, edges):
        replacements = {k: e for k, e in edges.items() if e["kind"] == "SUPERSEDES"}
        super()._semantics(nodes, {k: e for k, e in edges.items() if k not in replacements})
        if replacements:
            surrogate = copy.deepcopy(nodes)
            for edge in replacements.values():
                left = (edge["source"]["kind"], edge["source"]["key"])
                right = (edge["target"]["kind"], edge["target"]["key"])
                require(left in nodes and right in nodes, "invalid_graph_record", 409)
                approval = nodes.get(("Release", edge["payload"]["approval_ref"]))
                if not all(
                    nodes[k]["payload"].get("purpose") == "management_test" for k in (left, right)
                ):
                    require(
                        approval is not None
                        and approval["payload"].get("management_schema") == 1
                        and approval["payload"].get("authorized") is True
                        and approval["payload"]["approved_record"]["purpose"] == "actual",
                        "replacement_actual_required",
                        409,
                    )
                for key in (left, right):
                    surrogate[key]["payload"]["purpose"] = "management_test"
            # Preserve old cycle, identity and approval checks without changing its producer bytes.
            super()._semantics(surrogate, edges)
        for key, node in nodes.items():
            payload = node["payload"]
            if payload.get("management_schema") != 1:
                continue
            outgoing = [
                edge
                for edge in edges.values()
                if (edge["source"]["kind"], edge["source"]["key"]) == key
            ]
            if node["kind"] == "Validation":
                report = payload["report"]
                owner = ("SkillRevision", identity(report["skillId"], report["revision"]))
                require(owner in nodes, "invalid_graph_record")
                candidate = nodes[owner]["payload"]["candidate"]
                expected = validate_report(
                    candidate,
                    {
                        k: v
                        for k, v in report.items()
                        if k not in {"skillId", "revision", "validationId"}
                    },
                )
                require(expected == report, "invalid_graph_record")
                require(
                    {
                        (e["target"]["kind"], e["target"]["key"])
                        for e in outgoing
                        if e["kind"] == "EVALUATES"
                    }
                    == {owner},
                    "invalid_graph_record",
                )
                require(
                    {e["target"]["key"] for e in outgoing if e["kind"] == "USES_RUN"}
                    == {report["runId"]},
                    "invalid_graph_record",
                )
            elif node["kind"] == "Release":
                approved = payload["approved_record"]
                require(
                    {e["target"]["key"] for e in outgoing if e["kind"] == "PUBLISHES"}
                    == {identity(approved["skillId"], approved["revision"])},
                    "invalid_graph_record",
                )
                require(
                    {e["target"]["key"] for e in outgoing if e["kind"] == "BASED_ON"}
                    == set(approved["validationIds"]),
                    "invalid_graph_record",
                )

    def _record_uid(self, category, key):
        return hash_json([self.project_id, "management", category, key])

    def _records(self, tx):
        rows = list(
            tx.run(
                "MATCH (n:SkillManagementRecord {project_id:$project_id}) RETURN properties(n) AS n ORDER BY n.uid LIMIT $limit",
                project_id=self.project_id,
                limit=self.max_management_records + 1,
            )
        )
        require(len(rows) <= self.max_management_records, "size_limit", 409)
        records = {}
        for row in rows:
            item = dict(row["n"])
            category, key = item["category"], item["key"]
            require(
                category in _CATEGORIES and item.get("project_id") == self.project_id,
                "stored_content_mismatch",
                409,
            )
            payload = json.loads(item["payload_json"])
            require(
                item["uid"] == self._record_uid(category, key)
                and item["payload_sha256"] == hash_json(payload),
                "stored_content_mismatch",
                409,
            )
            require((category, key) not in records, "stored_content_mismatch", 409)
            records[(category, key)] = payload
        return records

    def _put(self, records, category, key, value, *, mutable=False):
        require(category in _CATEGORIES)
        value = public_copy(value)
        old = records.get((category, key))
        require(old is None or mutable or old == value, "immutable_conflict", 409)
        records[(category, key)] = value

    def _state(self, records, skill_id, revision):
        key = identity(skill_id, revision)
        value = records.get(("state", key))
        require(value is not None, "not_found", 404)
        return value

    def _items(self, records, category, state):
        candidate = state["candidate"]
        return [
            value
            for (kind, _), value in records.items()
            if kind == category
            and value.get("skillId") == candidate["skill_id"]
            and value.get("revision") == candidate["revision"]
        ]

    def _changed(self, records, state, action, actor, reason=None, *, details=None):
        self._execution_ordinal(state)
        state["sequence"] += 1
        state["updatedAt"] = now()
        event = {
            "skillId": state["candidate"]["skill_id"],
            "revision": state["candidate"]["revision"],
            "sequence": state["sequence"],
            "action": action,
            "actor": actor,
            "reason": reason,
            "timestamp": state["updatedAt"],
        }
        if details is not None:
            event["details"] = details
        self._put(records, "event", hash_json(event), event)
        self._propagate_lifecycle(records, state, actor, action)

    def _core_node(self, nodes, kind, payload):
        key = node_key(kind, payload)
        value = self._node({"kind": kind, "key": key, "payload": payload})
        require((kind, key) not in nodes or nodes[(kind, key)] == value, "immutable_conflict", 409)
        nodes[(kind, key)] = value
        return {"kind": kind, "key": key}

    def _core_edge(self, edges, kind, left, right, payload=None):
        payload = payload or {}
        value = self._edge(
            {
                "kind": kind,
                "key": edge_key(kind, left, right, payload),
                "source": left,
                "target": right,
                "payload": payload,
            }
        )
        key = (kind, value["key"])
        require(key not in edges or edges[key] == value, "immutable_conflict", 409)
        edges[key] = value

    def _run(self, callback, *, mutation=False, op=None):
        self.initialize()
        try:
            with self._driver.session(database=self.database) as session:
                self._database_guard(session)
                with session.begin_transaction(timeout=30) as tx:
                    # Reads that authorize use also take this lock, giving them a
                    # consistent state/release view relative to publication.
                    tx.run(
                        "MERGE (g:SkillGraphScope {project_id:$project_id}) ON CREATE SET g.sequence=0 SET g.sequence=g.sequence+1 RETURN g.sequence",
                        project_id=self.project_id,
                    ).consume()
                    nodes, edges = self._read(tx)
                    records = self._records(tx)
                    old_nodes, old_edges, old_records = copy.deepcopy((nodes, edges, records))
                    if op is not None:
                        op_id, request = op
                        operation(op_id)
                        previous = records.get(("operation", op_id))
                        if previous is not None:
                            require(
                                previous["requestHash"] == hash_json(request),
                                "operation_conflict",
                                409,
                            )
                            tx.commit()
                            return copy.deepcopy(previous["result"]), True
                    result = callback(records, nodes, edges)
                    if mutation:
                        if op is not None:
                            self._put(
                                records,
                                "operation",
                                op_id,
                                {
                                    "requestHash": hash_json(request),
                                    "result": result,
                                    "recordedAt": now(),
                                },
                            )
                        require(len(records) <= self.max_management_records, "size_limit", 409)
                        require(
                            len(nodes) <= self.max_nodes and len(edges) <= self.max_edges,
                            "size_limit",
                            409,
                        )
                        self._semantics(nodes, edges)
                        for key, node in nodes.items():
                            if key in old_nodes:
                                require(node == old_nodes[key], "immutable_conflict", 409)
                                continue
                            tx.run(
                                f"CREATE (n:SkillGraphEntity:{node['kind']}) SET n=$record",
                                record=self._record("node", node),
                            ).consume()
                        for key, edge in edges.items():
                            if key in old_edges:
                                require(edge == old_edges[key], "immutable_conflict", 409)
                                continue
                            value = self._record("edge", edge)
                            tx.run(
                                f"MATCH (a:SkillGraphEntity {{uid:$source,project_id:$project}}),(b:SkillGraphEntity {{uid:$target,project_id:$project}}) CREATE (a)-[r:{edge['kind']}]->(b) SET r=$record",
                                source=value["source_uid"],
                                target=value["target_uid"],
                                project=self.project_id,
                                record=value,
                            ).consume()
                        for (category, key), value in records.items():
                            if old_records.get((category, key)) == value:
                                continue
                            require(
                                (category, key) not in old_records or category in {"state", "job"},
                                "immutable_conflict",
                                409,
                            )
                            record = {
                                "uid": self._record_uid(category, key),
                                "project_id": self.project_id,
                                "category": category,
                                "key": key,
                                "payload_json": json.dumps(
                                    value,
                                    sort_keys=True,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    allow_nan=False,
                                ),
                                "payload_sha256": hash_json(value),
                            }
                            tx.run(
                                "MERGE (n:SkillManagementRecord {uid:$uid}) SET n=$record",
                                uid=record["uid"],
                                record=record,
                            ).consume()
                    tx.commit()
                    return public_copy(result), False
        except ManagementError:
            raise
        except ValueError:
            raise ManagementError("graph_contract_failed", 409) from None
        except Exception:
            raise ManagementError("storage_unavailable", 503) from None

    def _detail(self, records, nodes, state):
        validations = self._items(records, "validation", state)
        jobs = self._items(records, "job", state)
        candidate = state["candidate"]
        releases = [
            n["payload"]["approved_record"]
            for n in nodes.values()
            if n["kind"] == "Release"
            and n["payload"].get("management_schema") == 1
            and n["payload"]["approved_record"]["skillId"] == candidate["skill_id"]
            and n["payload"]["approved_record"]["revision"] == candidate["revision"]
        ]
        return {
            "candidate": candidate,
            "management": {
                **summary(self._lifecycle_state(records, nodes, state), validations, jobs),
                "challenges": state["challenges"],
                "releaseId": state.get("releaseId"),
                "exportHash": state.get("exportHash"),
                "projectionVersion": state.get("projectionVersion"),
            },
            "validations": validations,
            "jobs": jobs,
            "releases": releases,
            "governance": self._governance_detail(records, state),
            "lifecycle": self._lifecycle_projection(records, nodes, state),
            "artifactDirectory": state["artifactDirectory"],
        }

    def register_candidate(self, candidate, manifest, artifact_directory, *, challenges=None):
        validate_registration(candidate, manifest, self.project_id)
        directory = str(Path(text(str(artifact_directory))).absolute())
        challenges = [] if challenges is None else challenges
        require(isinstance(challenges, list) and len(challenges) <= 100)
        for reason in challenges:
            text(reason)

        def perform(records, nodes, edges):
            key = identity(candidate["skill_id"], candidate["revision"])
            core = nodes.get(("SkillRevision", key))
            require(
                core is not None and core["payload"].get("candidate") == candidate,
                "candidate_graph_missing",
                409,
            )
            old = records.get(("state", key))
            if old is not None:
                require(
                    old["candidate"] == candidate
                    and old["manifest"] == manifest
                    and old["artifactDirectory"] == directory,
                    "immutable_conflict",
                    409,
                )
                require(
                    set(challenges) <= {item["reason"] for item in old["challenges"]},
                    "immutable_conflict",
                    409,
                )
                return self._detail(records, nodes, old)
            created = now()
            state = {
                "candidate": candidate,
                "manifest": manifest,
                "artifactDirectory": directory,
                "state": "candidate",
                "sequence": 0,
                "sourceWatermark": sorted(
                    source["source_id"] for source in candidate["source_refs"]
                ),
                "exportHash": None,
                "projectionVersion": None,
                "releaseId": None,
                "updatedAt": created,
                "challenges": [
                    {
                        "challengeId": hash_json([key, reason]),
                        "reason": reason,
                        "severity": "needs_evidence",
                        "status": "open",
                        "actor": "registered_source_review",
                        "createdAt": created,
                    }
                    for reason in challenges
                ],
            }
            self._put(records, "state", key, state)
            return self._detail(records, nodes, records[("state", key)])

        return self._run(perform, mutation=True)[0]

    def list_candidates(self, query=""):
        require(isinstance(query, str) and len(query) <= 300)

        def read(records, nodes, edges):
            items = [
                summary(
                    self._lifecycle_state(records, nodes, value),
                    self._items(records, "validation", value),
                    self._items(records, "job", value),
                )
                for (kind, _), value in records.items()
                if kind == "state"
            ]
            items = [
                item
                for item in items
                if query.casefold()
                in " ".join([item["skillId"], item["title"], *item["tags"]]).casefold()
            ]
            items.sort(key=lambda item: (item["skillId"], item["revision"]))
            return {"items": items, "total": len(items)}

        return self._run(read)[0]

    def get_revision(self, skill_id, revision):
        return self._run(
            lambda records, nodes, edges: self._detail(
                records, nodes, self._state(records, skill_id, revision)
            )
        )[0]

    def bind_export(
        self,
        skill_id,
        revision,
        *,
        export_hash,
        projection_version,
        expected_sequence,
        operation_id,
        actor,
        reason,
    ):
        """Host-only projection migration; published exports are permanently frozen.

        Old jobs and validations retain their original export hashes. A corrected
        projection requires fresh runs and never retroactively repairs evidence.
        """
        key = identity(skill_id, revision)
        digest(export_hash)
        text(projection_version, maximum=80)
        operation(projection_version)
        text(actor, maximum=300)
        text(reason)
        request = {
            "action": "bind_export",
            "skillId": skill_id,
            "revision": revision,
            "exportHash": export_hash,
            "projectionVersion": projection_version,
            "expectedSequence": expected_sequence,
            "actor": actor,
            "reason": reason,
        }

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            self._check_sequence(state, expected_sequence)
            previously_published = any(
                edge["kind"] == "PUBLISHES"
                and edge["target"] == {"kind": "SkillRevision", "key": key}
                for edge in edges.values()
            )
            require(
                not state.get("releaseId")
                and not previously_published
                and state["state"] not in {"published", "paused", "withdrawn"},
                "export_frozen",
                409,
            )
            require(
                not any(
                    job["status"] in {"queued", "running"}
                    for job in self._items(records, "job", state)
                ),
                "evaluation_pending",
                409,
            )
            transition = {
                "previousExportHash": state.get("exportHash"),
                "previousProjectionVersion": state.get("projectionVersion"),
                "exportHash": export_hash,
                "projectionVersion": projection_version,
            }
            state.update(exportHash=export_hash, projectionVersion=projection_version)
            self._changed(records, state, "export_rebound", actor, reason, details=transition)
            return self._detail(records, nodes, state)

        return self._run(perform, mutation=True, op=(operation_id, request))[0]

    def record_validation(self, skill_id, revision, *, report, actor, operation_id):
        identity(skill_id, revision)
        text(actor, maximum=300)
        request = {
            "action": "record_validation",
            "skillId": skill_id,
            "revision": revision,
            "report": public_copy(report),
            "actor": actor,
        }

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            value = validate_report(state["candidate"], report)
            require(state["exportHash"] in (None, value["exportHash"]), "artifact_mismatch", 409)
            state["exportHash"] = value["exportHash"]
            self._put(records, "validation", value["validationId"], value)
            if state.get("lifecycleBinding"):
                context = self._validation_lifecycle_context(records, nodes, state)
                previous = state.setdefault("validationLifecycle", {}).get(value["validationId"])
                require(previous in (None, context), "validation_context_frozen", 409)
                state["validationLifecycle"][value["validationId"]] = context
            case = self._core_node(
                nodes,
                "CaseRevision",
                {
                    "case_id": value["caseId"],
                    "case_revision": value["caseRevision"],
                    "lineage_id": value["lineageId"],
                    "split": value["split"],
                    "management_schema": 1,
                },
            )
            run = self._core_node(
                nodes,
                "Run",
                {
                    "run_id": value["runId"],
                    "attempt_id": value["attemptId"],
                    "case_id": value["caseId"],
                    "case_revision": value["caseRevision"],
                    "group": value["kind"],
                },
            )
            validation = self._core_node(
                nodes,
                "Validation",
                {
                    "validation_id": value["validationId"],
                    "kind": value["kind"],
                    "status": value["status"],
                    "management_schema": 1,
                    "report": value,
                },
            )
            self._core_edge(edges, "OF_CASE", run, case)
            self._core_edge(edges, "USES_RUN", validation, run)
            self._core_edge(
                edges,
                "EVALUATES",
                validation,
                {"kind": "SkillRevision", "key": identity(skill_id, revision)},
            )
            if value.get("receipt"):
                evidence = self._core_node(
                    nodes,
                    "EvidenceRevision",
                    {
                        "evidence_revision": value["receipt"]["sha256"],
                        "uri": value["receipt"]["uri"],
                        "sha256": value["receipt"]["sha256"],
                        "run_id": value["runId"],
                        "attempt_id": value["attemptId"],
                        "case_id": value["caseId"],
                        "case_revision": value["caseRevision"],
                        "producer": "isolated_evaluator",
                    },
                )
                self._core_edge(edges, "FROM_RUN", evidence, run)
            self._changed(records, state, "validation_recorded", actor)
            return {**value, "sequence": state["sequence"]}

        return self._run(perform, mutation=True, op=(operation_id, request))[0]

    def _check_sequence(self, state, expected):
        require(type(expected) is int and expected >= 0)
        require(
            state["sequence"] == expected,
            "state_conflict",
            409,
            {"currentSequence": state["sequence"]},
        )

    def _check_watermark(self, state, watermark):
        require(isinstance(watermark, list) and all(isinstance(value, str) for value in watermark))
        require(watermark == state["sourceWatermark"], "source_changed", 409)

    def prepare_review(self, skill_id, revision, *, source_watermark, actor):
        text(actor, maximum=300)

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            self._check_watermark(state, source_watermark)
            validations = self._items(records, "validation", state)
            candidate = state["candidate"]
            covered = {
                branch
                for item in qualified_validations(state, validations)
                for branch in item["branchCoverage"]
            }
            package = {
                "projectId": self.project_id,
                "skillId": skill_id,
                "revision": revision,
                "sequence": state["sequence"],
                "contentHash": candidate["content_hash"],
                "manifestId": state["manifest"]["manifest_id"],
                "exportHash": state["exportHash"],
                "sourceWatermark": state["sourceWatermark"],
                "validationIds": sorted(item["validationId"] for item in validations),
                "assessmentIds": sorted(state.get("currentAssessments", {}).values()),
                "adjudicationIds": state.get("qualification", {}).get("activeAdjudicationIds", []),
                "readiness": readiness(
                    self._lifecycle_state(records, nodes, state),
                    validations,
                    self._items(records, "job", state),
                ),
                "reviewChecklist": [
                    {
                        "ruleId": rule["rule_id"],
                        "branchCount": len(rule["branches"]),
                        "coveredBranches": sorted(
                            branch for branch in covered if branch.startswith(rule["rule_id"] + "#")
                        ),
                    }
                    for rule in candidate["rules"]
                ],
                "actor": actor,
                "preparedAt": now(),
            }
            package.update(self._lifecycle_release_binding(records, nodes, state))
            package["packageId"] = hash_json(package)
            package["packageHash"] = hash_json(package)
            self._put(records, "package", package["packageId"], package)
            return package

        return self._run(perform, mutation=True)[0]

    def publish(
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
    ):
        digest(package_id)
        digest(package_hash)
        text(actor, maximum=300)
        text(reason)
        request = {
            "action": "publish",
            "skillId": skill_id,
            "revision": revision,
            "packageId": package_id,
            "packageHash": package_hash,
            "expectedSequence": expected_sequence,
            "actor": actor,
            "reason": reason,
            "sourceWatermark": source_watermark,
        }

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            self._check_sequence(state, expected_sequence)
            self._check_watermark(state, source_watermark)
            package = records.get(("package", package_id))
            require(
                package is not None
                and package["packageHash"] == package_hash
                and package["skillId"] == skill_id
                and package["revision"] == revision
                and package["actor"] == actor,
                "review_mismatch",
                409,
            )
            require(
                package["sequence"] == state["sequence"]
                and package["contentHash"] == state["candidate"]["content_hash"]
                and package["manifestId"] == state["manifest"]["manifest_id"]
                and package["exportHash"] == state["exportHash"]
                and package["sourceWatermark"] == state["sourceWatermark"],
                "review_mismatch",
                409,
            )
            validations = self._items(records, "validation", state)
            require(
                package["validationIds"] == sorted(item["validationId"] for item in validations),
                "review_mismatch",
                409,
            )
            binding = self._lifecycle_release_binding(records, nodes, state)
            require(all(package.get(k) == v for k, v in binding.items()), "review_mismatch", 409)
            gate = readiness(
                self._lifecycle_state(records, nodes, state),
                validations,
                self._items(records, "job", state),
            )
            require(
                gate["status"] == "PASS" and package["readiness"]["status"] == "PASS",
                "not_ready",
                422,
                gate,
            )
            require(state["state"] in {"candidate", "needs_evidence"}, "invalid_transition", 409)
            independent = qualified_validations(state, validations)
            purpose = (
                "management_test"
                if any(item["purpose"] == "management_test" for item in independent)
                else "actual"
            )
            if purpose == "actual":
                require(
                    all(
                        nodes[("Release", d["releaseId"])]["payload"]["approved_record"]["purpose"]
                        == "actual"
                        for d in binding.get("dependencyReleases", [])
                    ),
                    "dependency_actual_release_required",
                    422,
                )
            approved = {
                "projectId": self.project_id,
                "skillId": skill_id,
                "revision": revision,
                "contentHash": state["candidate"]["content_hash"],
                "manifestId": state["manifest"]["manifest_id"],
                "exportHash": state["exportHash"],
                "sourceWatermark": state["sourceWatermark"],
                "validationIds": package["validationIds"],
                "assessmentIds": package.get("assessmentIds", []),
                "adjudicationIds": package.get("adjudicationIds", []),
                "packageId": package_id,
                "packageHash": package_hash,
                "approvedSequence": state["sequence"],
                "actor": actor,
                "reason": reason,
                "purpose": purpose,
                "scope": {
                    "caseIds": sorted(
                        {case for item in independent for case in item["scope"]["caseIds"]}
                    ),
                    "agent": "claude_code",
                    "mode": "controlled_task",
                },
                "approvedAt": now(),
            }
            approved.update(binding)
            approved["releaseId"] = hash_json(approved)
            release = self._core_node(
                nodes,
                "Release",
                {
                    "release_id": approved["releaseId"],
                    "purpose": purpose,
                    "authorized": True,
                    "management_schema": 1,
                    "approved_record": approved,
                },
            )
            self._core_edge(
                edges,
                "PUBLISHES",
                release,
                {"kind": "SkillRevision", "key": identity(skill_id, revision)},
            )
            for validation_id in package["validationIds"]:
                self._core_edge(
                    edges, "BASED_ON", release, {"kind": "Validation", "key": validation_id}
                )
            state.update(state="published", releaseId=approved["releaseId"])
            self._changed(records, state, "published", actor, reason)
            return {
                "state": state["state"],
                "sequence": state["sequence"],
                "releaseId": state["releaseId"],
            }

        return self._run(perform, mutation=True, op=(operation_id, request))[0]

    def _disposition(
        self, skill_id, revision, *, action, expected_sequence, operation_id, actor, reason
    ):
        text(actor, maximum=300)
        text(reason)
        request = {
            "action": action,
            "skillId": skill_id,
            "revision": revision,
            "expectedSequence": expected_sequence,
            "actor": actor,
            "reason": reason,
        }

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            self._check_sequence(state, expected_sequence)
            if action in {"pause", "withdraw"}:
                require(
                    state.get("releaseId") and state["state"] in {"published", "paused"},
                    "invalid_transition",
                    409,
                )
                state["state"] = "paused" if action == "pause" else "withdrawn"
                if action == "pause":
                    state["pausedExecutionSequence"] = (
                        max(state.get("lifecycleOrdinal", 0), state["sequence"]) + 1
                    )
                    state["pausedValidationIds"] = sorted(
                        v["validationId"] for v in self._items(records, "validation", state)
                    )
            else:
                challenge = {
                    "reason": reason,
                    "severity": "needs_evidence" if action == "needs_evidence" else "block",
                    "status": "open",
                    "actor": actor,
                    "createdAt": now(),
                    "sequence": state["sequence"],
                }
                challenge["challengeId"] = hash_json(challenge)
                state["challenges"].append(challenge)
                if state.get("releaseId"):
                    state["pausedExecutionSequence"] = (
                        max(state.get("lifecycleOrdinal", 0), state["sequence"]) + 1
                    )
                    state["pausedValidationIds"] = sorted(
                        v["validationId"] for v in self._items(records, "validation", state)
                    )
                if state["state"] != "withdrawn":
                    state["state"] = (
                        "paused"
                        if state.get("releaseId")
                        else "needs_evidence"
                        if action == "needs_evidence"
                        else "blocked"
                    )
            self._changed(records, state, action, actor, reason)
            return {
                "state": state["state"],
                "sequence": state["sequence"],
                "releaseId": state.get("releaseId"),
            }

        return self._run(perform, mutation=True, op=(operation_id, request))[0]

    def decide_review(
        self, skill_id, revision, *, decision, expected_sequence, operation_id, actor, reason
    ):
        require(decision in {"needs_evidence", "block"})
        return self._disposition(
            skill_id,
            revision,
            action=decision,
            expected_sequence=expected_sequence,
            operation_id=operation_id,
            actor=actor,
            reason=reason,
        )

    def set_status(
        self, skill_id, revision, *, action, expected_sequence, operation_id, actor, reason
    ):
        require(action in {"pause", "withdraw"})
        return self._disposition(
            skill_id,
            revision,
            action=action,
            expected_sequence=expected_sequence,
            operation_id=operation_id,
            actor=actor,
            reason=reason,
        )

    def challenge(self, skill_id, revision, *, expected_sequence, operation_id, actor, reason):
        return self._disposition(
            skill_id,
            revision,
            action="challenge",
            expected_sequence=expected_sequence,
            operation_id=operation_id,
            actor=actor,
            reason=reason,
        )

    def _admission(self, records, nodes, state, case_id, allow_management_test):
        candidate = state["candidate"]
        result = {
            "allowed": False,
            "reason": "release_unavailable",
            "releaseId": state.get("releaseId"),
            "sequence": state["sequence"],
            "exportHash": state["exportHash"],
            "contentHash": candidate["content_hash"],
            "scope": None,
        }
        if (
            state["state"] != "published"
            or not state.get("releaseId")
            or any(item["status"] == "open" for item in state["challenges"])
        ):
            return result
        # A pending additional run does not revoke an already granted release;
        # new contradictory facts or invalidated adjudication do revoke it.
        if (
            readiness(
                self._lifecycle_state(records, nodes, state),
                self._items(records, "validation", state),
                [],
            )["status"]
            != "PASS"
        ):
            result["reason"] = "qualification_unavailable"
            return result
        node = nodes.get(("Release", state["releaseId"]))
        require(
            node is not None and node["payload"].get("authorized") is True,
            "stored_content_mismatch",
            409,
        )
        approved = node["payload"]["approved_record"]
        require(
            approved["skillId"] == candidate["skill_id"]
            and approved["revision"] == candidate["revision"]
            and approved["contentHash"] == candidate["content_hash"]
            and approved["exportHash"] == state["exportHash"]
            and approved["manifestId"] == state["manifest"]["manifest_id"]
            and approved["sourceWatermark"] == state["sourceWatermark"],
            "stored_content_mismatch",
            409,
        )
        result["scope"] = approved["scope"]
        if approved["purpose"] == "management_test" and not allow_management_test:
            result["reason"] = "management_test_only"
        elif case_id not in approved["scope"]["caseIds"]:
            result["reason"] = "outside_release_scope"
        else:
            result.update(allowed=True, reason="admitted")
        return result

    def admission(self, skill_id, revision, case_id, *, allow_management_test=False):
        text(case_id, maximum=300)
        require(type(allow_management_test) is bool)
        return self._run(
            lambda records, nodes, edges: self._admission(
                records,
                nodes,
                self._state(records, skill_id, revision),
                case_id,
                allow_management_test,
            )
        )[0]

    @staticmethod
    def _job_request(skill_id, revision, *, case_id, actor, export_hash, mode):
        identity(skill_id, revision)
        text(case_id, maximum=300)
        if case_id.startswith(("release-", "p5-")):
            if case_id.startswith("p5-"):
                from memos.skill_p5.service_runtime import dev_case
            else:
                from memos.skill_release.service_runtime import dev_case

            try:
                dev_case(case_id)
            except ValueError:
                raise ManagementError("invalid_input") from None
        else:
            require(case_id.startswith("dev-"))
        require(mode in ("candidate_evaluation", "normal"))
        digest(export_hash)
        text(actor, maximum=300)
        return {
            "action": "create_job",
            "skillId": skill_id,
            "revision": revision,
            "caseId": case_id,
            "actor": actor,
            "exportHash": export_hash,
            "mode": mode,
        }

    def find_job(
        self,
        skill_id,
        revision,
        *,
        case_id,
        actor,
        operation_id,
        export_hash,
        mode="candidate_evaluation",
    ):
        """Resolve durable idempotency before considering local execution capacity."""
        operation(operation_id)
        request = self._job_request(
            skill_id, revision, case_id=case_id, actor=actor, export_hash=export_hash, mode=mode
        )

        def read(records, nodes, edges):
            previous = records.get(("operation", operation_id))
            if previous is None:
                return None
            require(previous["requestHash"] == hash_json(request), "operation_conflict", 409)
            return self._job(records, previous["result"]["jobId"])

        return self._run(read)[0]

    def create_job(
        self,
        skill_id,
        revision,
        *,
        case_id,
        actor,
        operation_id,
        export_hash,
        mode="candidate_evaluation",
    ):
        request = self._job_request(
            skill_id, revision, case_id=case_id, actor=actor, export_hash=export_hash, mode=mode
        )

        def perform(records, nodes, edges):
            state = self._state(records, skill_id, revision)
            require(state["exportHash"] in (None, export_hash), "artifact_mismatch", 409)
            if mode == "normal":
                gate = self._admission(records, nodes, state, case_id, False)
                require(gate["allowed"], "release_unavailable", 403, {"reason": gate["reason"]})
            state["exportHash"] = export_hash
            job = {
                "jobId": hash_json([self.project_id, operation_id, request]),
                "skillId": skill_id,
                "revision": revision,
                "caseId": case_id,
                "mode": mode,
                "actor": actor,
                "status": "queued",
                "exportHash": export_hash,
                "contentHash": state["candidate"]["content_hash"],
                "taskResult": None,
                "skillUsage": None,
                "validationId": None,
                "errorCode": None,
                "createdAt": now(),
                "startedAt": None,
                "finishedAt": None,
            }
            self._put(records, "job", job["jobId"], job)
            self._changed(records, state, "evaluation_queued", actor)
            return job

        job, replayed = self._run(perform, mutation=True, op=(operation_id, request))
        if replayed:
            job = self.get_job(job["jobId"])
        return {**job, "created": not replayed}

    def _job(self, records, job_id):
        digest(job_id)
        job = records.get(("job", job_id))
        require(job is not None, "not_found", 404)
        return job

    def get_job(self, job_id):
        return self._run(lambda records, nodes, edges: self._job(records, job_id))[0]

    def claim_job(self, job_id):
        def perform(records, nodes, edges):
            job = self._job(records, job_id)
            if job["status"] != "queued":
                return None
            job.update(status="running", startedAt=now())
            return job

        return self._run(perform, mutation=True)[0]

    def finish_job(
        self,
        job_id,
        *,
        status,
        task_result=None,
        skill_usage=None,
        validation_id=None,
        error_code=None,
    ):
        require(status in {"passed", "failed", "unknown"})
        outcome = public_copy(
            {
                "status": status,
                "taskResult": task_result,
                "skillUsage": skill_usage,
                "validationId": validation_id,
                "errorCode": error_code,
            }
        )

        def perform(records, nodes, edges):
            job = self._job(records, job_id)
            if job["status"] in {"passed", "failed", "unknown"}:
                require(
                    all(job.get(key) == value for key, value in outcome.items()),
                    "job_terminal",
                    409,
                )
                return job
            # A dispatch failure has no execution to claim. Persist it explicitly
            # rather than leaving a queued job that could later run accidentally.
            require(
                job["status"] == "running"
                or (job["status"] == "queued" and status == "unknown" and validation_id is None),
                "job_not_running",
                409,
            )
            if validation_id is not None:
                validation = records.get(("validation", digest(validation_id)))
                require(
                    validation is not None
                    and validation["skillId"] == job["skillId"]
                    and validation["revision"] == job["revision"]
                    and validation["caseId"] == job["caseId"]
                    and validation["exportHash"] == job["exportHash"]
                    and validation["status"] == status
                    and validation["taskResult"] == task_result
                    and validation["skillUsage"] == skill_usage,
                    "validation_mismatch",
                    409,
                )
            if status == "passed":
                require(
                    validation_id is not None and validation["status"] == "passed",
                    "validation_mismatch",
                    409,
                )
            job.update(outcome, finishedAt=now())
            state = self._state(records, job["skillId"], job["revision"])
            self._changed(records, state, "evaluation_finished", job["actor"])
            return job

        return self._run(perform, mutation=True)[0]

    def recover_jobs(self):
        def perform(records, nodes, edges):
            count = 0
            for (category, _), job in list(records.items()):
                if category == "job" and job["status"] in {"queued", "running"}:
                    job.update(status="unknown", errorCode="service_restarted", finishedAt=now())
                    state = self._state(records, job["skillId"], job["revision"])
                    self._changed(records, state, "evaluation_interrupted", "service_recovery")
                    count += 1
            return count

        return self._run(perform, mutation=True)[0]
