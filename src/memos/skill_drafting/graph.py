"""Immutable, project-scoped Skill Graph on a separate Neo4j database.

The repository is not a publication authority. Its structural Validation nodes
and synthetic Release fixtures do not grant permission to execute a Skill.
"""

from __future__ import annotations

import hashlib
import json
import re

from collections import defaultdict, deque
from urllib.parse import urlsplit


NODE_KINDS = frozenset(
    {
        "Skill",
        "SkillRevision",
        "Rule",
        "MemorySnapshot",
        "CaseRevision",
        "Run",
        "EvidenceRevision",
        "Validation",
        "Release",
    }
)
EDGE_ENDPOINTS = {
    "HAS_REVISION": ("Skill", "SkillRevision"),
    "HAS_RULE": ("SkillRevision", "Rule"),
    "DERIVED_FROM": ("SkillRevision", "MemorySnapshot"),
    "REFERENCES": ("MemorySnapshot", "EvidenceRevision"),
    "EVIDENCE_LINK": ("Rule", "EvidenceRevision"),
    "FROM_RUN": ("EvidenceRevision", "Run"),
    "OF_CASE": ("Run", "CaseRevision"),
    "EVALUATES": ("Validation", "SkillRevision"),
    "USES_RUN": ("Validation", "Run"),
    "PUBLISHES": ("Release", "SkillRevision"),
    "BASED_ON": ("Release", "Validation"),
    "RELATED_TO": ("Skill", "Skill"),
    "REQUIRES": ("SkillRevision", "SkillRevision"),
    "SUPERSEDES": ("SkillRevision", "SkillRevision"),
}
_REQUIRED = {
    "Skill": ("skill_id",),
    "SkillRevision": ("skill_id", "revision", "status"),
    "Rule": ("skill_id", "revision", "rule_id"),
    "MemorySnapshot": ("snapshot_id", "memory_id", "content_sha256", "evidence_revision"),
    "CaseRevision": ("case_id", "case_revision", "lineage_id", "split"),
    "Run": ("run_id", "case_id", "case_revision", "attempt_id"),
    "EvidenceRevision": (
        "evidence_revision",
        "uri",
        "sha256",
        "run_id",
        "attempt_id",
        "case_id",
        "case_revision",
    ),
    "Validation": ("validation_id", "kind", "status"),
    "Release": ("release_id",),
}
_READ_NODES = """/* sg:nodes */
MATCH (n:SkillGraphEntity {project_id:$project_id}) RETURN properties(n) AS n
ORDER BY n.uid LIMIT $limit"""
_READ_EDGES = """/* sg:edges */
MATCH (a:SkillGraphEntity)-[r]->(b:SkillGraphEntity)
WHERE a.project_id=$project_id OR b.project_id=$project_id OR r.project_id=$project_id
RETURN properties(r) AS r, properties(a) AS source, properties(b) AS target,
type(r) AS relationship_type ORDER BY r.uid LIMIT $limit"""


def _json(value) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _require(condition, code):
    if not condition:
        raise ValueError("graph_" + code)


def _text(value) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 1000
        and "\x00" not in value
    )


def node_key(kind: str, payload: dict) -> str:
    """Canonical logical identities prevent aliases from duplicating a revision."""
    _require(
        isinstance(kind, str) and kind in NODE_KINDS and isinstance(payload, dict), "invalid_node"
    )
    _require(all(_text(payload.get(key)) for key in _REQUIRED[kind]), "node_fields_missing")
    if kind == "SkillRevision":
        return f"{payload['skill_id']}@{payload['revision']}"
    if kind == "Rule":
        return f"{payload['skill_id']}@{payload['revision']}#{payload['rule_id']}"
    if kind == "CaseRevision":
        return f"{payload['case_id']}@{payload['case_revision']}"
    field = {
        "Skill": "skill_id",
        "MemorySnapshot": "snapshot_id",
        "Run": "run_id",
        "EvidenceRevision": "evidence_revision",
        "Validation": "validation_id",
        "Release": "release_id",
    }[kind]
    return payload[field]


def _endpoint(value: dict) -> tuple[str, str]:
    _require(
        isinstance(value, dict)
        and set(value) == {"kind", "key"}
        and isinstance(value["kind"], str)
        and value["kind"] in NODE_KINDS
        and _text(value["key"]),
        "invalid_endpoint",
    )
    return value["kind"], value["key"]


def edge_key(kind: str, source: dict, target: dict, payload: dict) -> str:
    _require(
        isinstance(kind, str) and kind in EDGE_ENDPOINTS and isinstance(payload, dict),
        "invalid_edge",
    )
    left, right = _endpoint(source), _endpoint(target)
    if kind == "EVIDENCE_LINK":
        _require(_text(payload.get("link_id")), "evidence_link_fields_missing")
        return payload["link_id"]
    if kind == "RELATED_TO":
        left, right = sorted((left, right))
    return _hash([kind, left, right])


def _acyclic(edges: list[dict], kind: str) -> None:
    following = defaultdict(set)
    indegree = defaultdict(int)
    for edge in edges:
        if edge["kind"] != kind:
            continue
        left, right = _endpoint(edge["source"]), _endpoint(edge["target"])
        if right not in following[left]:
            following[left].add(right)
            indegree[right] += 1
            indegree.setdefault(left, 0)
    queue = deque(key for key, count in indegree.items() if not count)
    seen = 0
    while queue:
        current = queue.popleft()
        seen += 1
        for child in following[current]:
            indegree[child] -= 1
            if not indegree[child]:
                queue.append(child)
    _require(seen == len(indegree), "cycle")


class SkillGraphRepository:
    """Scoped immutable storage; the caller supplies trusted project context.

    All writes for one project acquire the same Neo4j write lock before reading
    its graph. Otherwise two concurrent dependency checks could both miss the
    edge being added by the other transaction and jointly introduce a cycle.
    """

    max_nodes = 2000
    max_edges = 10000
    max_payload_bytes = 2_000_000

    def __init__(
        self,
        uri: str,
        *,
        user: str,
        password: str,
        project_id: str,
        memory_database_id: str,
        database: str = "neo4j",
        driver=None,
    ):
        _require(
            isinstance(project_id, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project_id),
            "invalid_project",
        )
        _require(_text(memory_database_id) and _text(database), "invalid_database_configuration")
        try:
            parsed = urlsplit(uri)
            valid_uri = (
                parsed.scheme in {"neo4j", "bolt", "neo4j+s", "neo4j+ssc", "bolt+s", "bolt+ssc"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            )
            _ = parsed.port
        except (TypeError, ValueError):
            valid_uri = False
        _require(valid_uri and _text(user) and _text(password), "invalid_connection")
        self.project_id = project_id
        self.database = database
        self.memory_database_id = memory_database_id
        self._database_id = None
        self._initialized = False
        if driver is None:
            # Importing the optional domain never loads Neo4j or initializes the
            # existing Memory graph/vector-store adapters.
            try:
                from neo4j import GraphDatabase
            except ImportError:
                raise ImportError("Skill Graph requires the optional neo4j package") from None
            try:
                driver = GraphDatabase.driver(
                    uri,
                    auth=(user, password),
                    connection_timeout=10,
                    max_transaction_retry_time=0,
                )
            except Exception:
                raise ValueError("graph_invalid_connection") from None
        self._driver = driver

    def close(self) -> None:
        self._driver.close()

    def _database_guard(self, session) -> str:
        row = session.run("/* sg:database */ CALL db.info() YIELD id RETURN id").single()
        identity = row.get("id") if row else None
        _require(_text(identity), "database_unverified")
        _require(identity != self.memory_database_id, "memory_database_forbidden")
        _require(self._database_id in (None, identity), "database_changed")
        self._database_id = identity
        return identity

    def initialize(self) -> dict:
        try:
            with self._driver.session(database=self.database) as session:
                identity = self._database_guard(session)
                if not self._initialized:
                    statements = [
                        "CREATE CONSTRAINT memos_skill_entity_uid IF NOT EXISTS FOR (n:SkillGraphEntity) REQUIRE n.uid IS UNIQUE",
                        "CREATE CONSTRAINT memos_skill_scope_project IF NOT EXISTS FOR (n:SkillGraphScope) REQUIRE n.project_id IS UNIQUE",
                    ]
                    statements.extend(
                        f"CREATE CONSTRAINT memos_skill_{kind.lower()}_uid IF NOT EXISTS FOR ()-[r:{kind}]-() REQUIRE r.uid IS UNIQUE"
                        for kind in EDGE_ENDPOINTS
                    )
                    for query in statements:
                        session.run(query).consume()
                    self._initialized = True
            return {"database_id": identity, "project_id": self.project_id}
        except ValueError:
            raise
        except Exception:
            raise RuntimeError("graph_unavailable") from None

    def _uid(self, category: str, kind: str, key: str) -> str:
        return _hash([self.project_id, category, kind, key])

    def _node(self, node: dict) -> dict:
        _require(isinstance(node, dict) and set(node) == {"kind", "key", "payload"}, "invalid_node")
        kind, payload = node["kind"], node["payload"]
        canonical_key = node_key(kind, payload)
        _require(_text(node["key"]) and node["key"] == canonical_key, "identity_mismatch")
        _require(payload.get("project_id", self.project_id) == self.project_id, "project_mismatch")
        synthetic = payload.get("purpose") == "management_test"
        if kind == "SkillRevision":
            _require(payload["status"] == "candidate", "revision_not_candidate")
            requires = payload.get("requires", [])
            _require(isinstance(requires, list), "invalid_dependency_declaration")
            for required in requires:
                _require(
                    isinstance(required, dict)
                    and set(required) == {"skill_id", "revision"}
                    and all(_text(required[key]) for key in required),
                    "invalid_dependency_declaration",
                )
        if kind == "CaseRevision":
            _require(payload["split"] == "source" or synthetic, "non_source_case")
        if kind == "Rule" and "memory_citations" in payload:
            citations = payload["memory_citations"]
            _require(isinstance(citations, list), "invalid_memory_citation")
            _require(
                all(
                    isinstance(citation, dict)
                    and citation.get("target_kind") == "MemorySnapshot"
                    and _text(citation.get("target_id"))
                    for citation in citations
                ),
                "invalid_memory_citation",
            )
        if kind == "Validation" and not synthetic:
            _require(
                payload["kind"] == "structural" and payload.get("semantic_review") == "pending",
                "validation_not_structural",
            )
        if kind == "Release":
            _require(synthetic and payload.get("authorized") is False, "release_not_supported")
        try:
            serialized = _json(payload)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("graph_invalid_payload") from None
        _require(len(serialized.encode()) <= self.max_payload_bytes, "payload_too_large")
        return {"kind": kind, "key": node["key"], "payload": json.loads(serialized)}

    def _edge(self, edge: dict) -> dict:
        _require(
            isinstance(edge, dict) and set(edge) == {"kind", "key", "source", "target", "payload"},
            "invalid_edge",
        )
        kind, payload = edge["kind"], edge["payload"]
        _require(
            isinstance(kind, str) and kind in EDGE_ENDPOINTS and isinstance(payload, dict),
            "invalid_edge",
        )
        left, right = _endpoint(edge["source"]), _endpoint(edge["target"])
        _require((left[0], right[0]) == EDGE_ENDPOINTS[kind], "endpoint_type")
        _require(left != right, "self_reference")
        _require(
            edge["key"] == edge_key(kind, edge["source"], edge["target"], payload),
            "identity_mismatch",
        )
        _require(payload.get("project_id", self.project_id) == self.project_id, "project_mismatch")
        if kind == "EVIDENCE_LINK":
            _require(
                payload.get("kind") in {"supports", "contradicts", "precondition", "background"}
                and _text(payload.get("claim"))
                and _text(payload.get("producer"))
                and payload.get("semantic_review") == "pending"
                and (isinstance(payload.get("locator"), dict) or _text(payload.get("locator"))),
                "evidence_link_fields_missing",
            )
        if kind == "SUPERSEDES":
            _require(
                _text(payload.get("scope")) and _text(payload.get("approval_ref")),
                "replacement_approval_missing",
            )
        try:
            serialized = _json(payload)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("graph_invalid_payload") from None
        _require(len(serialized.encode()) <= self.max_payload_bytes, "payload_too_large")
        if kind == "RELATED_TO" and left > right:
            left, right = right, left
        return {
            "kind": kind,
            "key": edge["key"],
            "source": dict(zip(("kind", "key"), left, strict=True)),
            "target": dict(zip(("kind", "key"), right, strict=True)),
            "payload": json.loads(serialized),
        }

    def _semantics(self, nodes: dict, edges: dict) -> None:
        outgoing = defaultdict(list)
        owners = {}
        for relation in edges.values():
            left, right = _endpoint(relation["source"]), _endpoint(relation["target"])
            _require(left in nodes and right in nodes, "dangling_endpoint")
            a, b, kind = nodes[left]["payload"], nodes[right]["payload"], relation["kind"]
            outgoing[left].append(relation)
            # Provenance is an immutable identity chain, not an append-only set
            # of interchangeable parents. Alternative sources use new revisions.
            if kind in {"REFERENCES", "FROM_RUN", "OF_CASE", "EVALUATES"}:
                identity = (kind, left)
                _require(identity not in owners or owners[identity] == right, "ownership_mismatch")
                owners[identity] = right
            if kind == "HAS_REVISION":
                _require(a["skill_id"] == b["skill_id"], "ownership_mismatch")
            elif kind == "HAS_RULE":
                _require(
                    (a["skill_id"], a["revision"]) == (b["skill_id"], b["revision"]),
                    "ownership_mismatch",
                )
            elif kind == "REFERENCES":
                _require(a["evidence_revision"] == b["evidence_revision"], "ownership_mismatch")
            elif kind == "FROM_RUN":
                _require(
                    all(
                        a[key] == b[key]
                        for key in ("run_id", "attempt_id", "case_id", "case_revision")
                    ),
                    "ownership_mismatch",
                )
            elif kind == "OF_CASE":
                _require(
                    (a["case_id"], a["case_revision"]) == (b["case_id"], b["case_revision"]),
                    "ownership_mismatch",
                )
            elif kind == "EVIDENCE_LINK":
                binding = relation["payload"]
                expected = {
                    "target_kind": "EvidenceRevision",
                    "target_id": b["evidence_revision"],
                    "skill_id": a["skill_id"],
                    "revision": a["revision"],
                    "rule_id": a["rule_id"],
                }
                _require(
                    all(binding.get(key, value) == value for key, value in expected.items()),
                    "ownership_mismatch",
                )
                if "rule" in a:
                    _require(binding in a["rule"]["evidence_links"], "evidence_link_undeclared")
            elif kind == "REQUIRES" and a.get("purpose") != "management_test":
                _require(
                    {"skill_id": b["skill_id"], "revision": b["revision"]} in a.get("requires", []),
                    "dependency_undeclared",
                )
            elif kind == "SUPERSEDES":
                _require(a["skill_id"] == b["skill_id"], "replacement_skill_mismatch")
                _require(
                    a.get("purpose") == b.get("purpose") == "management_test",
                    "replacement_release_not_supported",
                )
                approval = nodes.get(("Release", relation["payload"]["approval_ref"]))
                _require(approval is not None, "replacement_approval_missing")
                _require(
                    any(
                        r["kind"] == "PUBLISHES"
                        and _endpoint(r["source"]) == ("Release", approval["key"])
                        and _endpoint(r["target"]) == left
                        for r in edges.values()
                    ),
                    "replacement_approval_mismatch",
                )
        for identity, node in nodes.items():
            payload = node["payload"]
            if node["kind"] == "Rule" and payload.get("memory_citations"):
                owner = ("SkillRevision", f"{payload['skill_id']}@{payload['revision']}")
                owned = any(
                    e["kind"] == "HAS_RULE" and _endpoint(e["target"]) == identity
                    for e in outgoing[owner]
                )
                derived = {
                    _endpoint(e["target"]) for e in outgoing[owner] if e["kind"] == "DERIVED_FROM"
                }
                _require(
                    owned
                    and all(
                        ("MemorySnapshot", citation["target_id"]) in derived
                        for citation in payload["memory_citations"]
                    ),
                    "memory_citation_unbound",
                )
            if node["kind"] == "SkillRevision" and payload.get("purpose") != "management_test":
                actual = {
                    (
                        nodes[_endpoint(e["target"])]["payload"]["skill_id"],
                        nodes[_endpoint(e["target"])]["payload"]["revision"],
                    )
                    for e in outgoing[identity]
                    if e["kind"] == "REQUIRES"
                }
                declared = {(v["skill_id"], v["revision"]) for v in payload.get("requires", [])}
                _require(actual == declared, "dependency_declaration_incomplete")
        _acyclic(list(edges.values()), "REQUIRES")
        _acyclic(list(edges.values()), "SUPERSEDES")

    def _record(self, category: str, item: dict) -> dict:
        record = {
            "uid": self._uid(category, item["kind"], item["key"]),
            "project_id": self.project_id,
            "kind": item["kind"],
            "key": item["key"],
            "payload_json": _json(item["payload"]),
            "payload_sha256": _hash(item["payload"]),
        }
        if category == "edge":
            record.update(
                source_uid=self._uid("node", **item["source"]),
                target_uid=self._uid("node", **item["target"]),
            )
        return record

    def _read(self, tx) -> tuple[dict, dict]:
        node_rows = list(tx.run(_READ_NODES, project_id=self.project_id, limit=self.max_nodes + 1))
        edge_rows = list(tx.run(_READ_EDGES, project_id=self.project_id, limit=self.max_edges + 1))
        _require(
            len(node_rows) <= self.max_nodes and len(edge_rows) <= self.max_edges, "size_limit"
        )
        nodes, edges = {}, {}
        for row in node_rows:
            raw = dict(row["n"])
            _require(raw.get("project_id") == self.project_id, "project_mismatch")
            item = self._node(
                {"kind": raw["kind"], "key": raw["key"], "payload": json.loads(raw["payload_json"])}
            )
            _require(
                all(raw.get(k) == v for k, v in self._record("node", item).items()),
                "stored_content_mismatch",
            )
            _require((item["kind"], item["key"]) not in nodes, "duplicate_identity")
            nodes[(item["kind"], item["key"])] = item
        for row in edge_rows:
            raw, left, right = (dict(row[key]) for key in ("r", "source", "target"))
            _require(
                all(v.get("project_id") == self.project_id for v in (raw, left, right)),
                "project_mismatch",
            )
            _require(row.get("relationship_type", raw["kind"]) == raw["kind"], "endpoint_type")
            item = self._edge(
                {
                    "kind": raw["kind"],
                    "key": raw["key"],
                    "payload": json.loads(raw["payload_json"]),
                    "source": {"kind": left["kind"], "key": left["key"]},
                    "target": {"kind": right["kind"], "key": right["key"]},
                }
            )
            _require(
                all(raw.get(k) == v for k, v in self._record("edge", item).items()),
                "stored_content_mismatch",
            )
            _require((item["kind"], item["key"]) not in edges, "duplicate_identity")
            edges[(item["kind"], item["key"])] = item
        self._semantics(nodes, edges)
        return nodes, edges

    def store_bundle(self, nodes: list[dict], edges: list[dict]) -> dict:
        _require(isinstance(nodes, list) and isinstance(edges, list), "invalid_bundle")
        additions = [self._node(item) for item in nodes]
        relations = [self._edge(item) for item in edges]
        self.initialize()
        try:
            with self._driver.session(database=self.database) as session:
                self._database_guard(session)
                with session.begin_transaction(timeout=30) as tx:
                    tx.run(
                        """/* sg:lock */
MERGE (g:SkillGraphScope {project_id:$project_id})
ON CREATE SET g.sequence=0 SET g.sequence=g.sequence+1 RETURN g.sequence AS sequence""",
                        project_id=self.project_id,
                    ).consume()
                    old_nodes, old_edges = self._read(tx)
                    combined_nodes, combined_edges = dict(old_nodes), dict(old_edges)
                    for item, combined in [(x, combined_nodes) for x in additions] + [
                        (x, combined_edges) for x in relations
                    ]:
                        identity = (item["kind"], item["key"])
                        _require(
                            identity not in combined or combined[identity] == item,
                            "immutable_conflict",
                        )
                        combined[identity] = item
                    _require(
                        len(combined_nodes) <= self.max_nodes
                        and len(combined_edges) <= self.max_edges,
                        "size_limit",
                    )
                    self._semantics(combined_nodes, combined_edges)
                    for identity, item in combined_nodes.items():
                        if identity in old_nodes:
                            continue
                        # Only prevalidated fixed labels/types enter Cypher text.
                        tx.run(
                            f"/* sg:put-node */ CREATE (n:SkillGraphEntity:{item['kind']}) SET n=$record",
                            record=self._record("node", item),
                        ).consume()
                    for identity, item in combined_edges.items():
                        if identity in old_edges:
                            continue
                        record = self._record("edge", item)
                        tx.run(
                            f"""/* sg:put-edge */
MATCH (a:SkillGraphEntity {{uid:$source_uid,project_id:$project_id}}),
(b:SkillGraphEntity {{uid:$target_uid,project_id:$project_id}})
CREATE (a)-[r:{item["kind"]}]->(b) SET r=$record""",
                            record=record,
                            source_uid=record["source_uid"],
                            target_uid=record["target_uid"],
                            project_id=self.project_id,
                        ).consume()
                    tx.commit()
            return {
                "status": "stored",
                "nodes_added": len(combined_nodes) - len(old_nodes),
                "edges_added": len(combined_edges) - len(old_edges),
                "node_count": len(combined_nodes),
                "edge_count": len(combined_edges),
            }
        except ValueError:
            raise
        except Exception:
            raise RuntimeError("graph_unavailable") from None

    def inspect(self) -> dict:
        self.initialize()
        try:
            with self._driver.session(database=self.database) as session:
                self._database_guard(session)
                with session.begin_transaction(timeout=30) as tx:
                    nodes, edges = self._read(tx)
            return {
                "project_id": self.project_id,
                "nodes": sorted(nodes.values(), key=lambda x: (x["kind"], x["key"])),
                "edges": sorted(edges.values(), key=lambda x: (x["kind"], x["key"])),
            }
        except ValueError:
            raise
        except Exception:
            raise RuntimeError("graph_unavailable") from None

    def get_node(self, kind: str, key: str) -> dict | None:
        _endpoint({"kind": kind, "key": key})
        return next(
            (
                node
                for node in self.inspect()["nodes"]
                if (node["kind"], node["key"]) == (kind, key)
            ),
            None,
        )

    def neighbors(self, kind: str, key: str, *, relation: str | None = None) -> list[dict]:
        _endpoint({"kind": kind, "key": key})
        _require(
            relation is None or (isinstance(relation, str) and relation in EDGE_ENDPOINTS),
            "invalid_edge",
        )
        graph = self.inspect()
        nodes = {(n["kind"], n["key"]): n for n in graph["nodes"]}
        result = []
        for edge in graph["edges"]:
            if relation and edge["kind"] != relation:
                continue
            left, right = _endpoint(edge["source"]), _endpoint(edge["target"])
            if (kind, key) in (left, right):
                other = right if (kind, key) == left else left
                result.append(
                    {
                        "edge": edge,
                        "node": nodes[other],
                        "direction": "undirected"
                        if edge["kind"] == "RELATED_TO"
                        else "out"
                        if (kind, key) == left
                        else "in",
                    }
                )
        return result

    def trace_rule(self, rule_key: str) -> dict:
        graph = self.inspect()
        nodes = {(n["kind"], n["key"]): n for n in graph["nodes"]}
        rule = nodes.get(("Rule", rule_key))
        _require(rule is not None, "node_not_found")
        selected = {("Rule", rule_key)}
        # Fixed provenance paths, not unrestricted arbitrary traversal.
        for relation, reverse in (
            ("EVIDENCE_LINK", False),
            ("FROM_RUN", False),
            ("OF_CASE", False),
            ("HAS_RULE", True),
        ):
            for edge in graph["edges"]:
                start = _endpoint(edge["target"] if reverse else edge["source"])
                end = _endpoint(edge["source"] if reverse else edge["target"])
                if edge["kind"] == relation and start in selected:
                    selected.add(end)
        # Several Memory observations can refer to one immutable Evidence.
        # Follow the rule's own citations, never all observations of that file.
        derived = {
            _endpoint(e["target"])
            for e in graph["edges"]
            if e["kind"] == "DERIVED_FROM" and _endpoint(e["source"]) in selected
        }
        citations = rule["payload"].get("memory_citations")
        if citations is not None:
            memories = {("MemorySnapshot", citation["target_id"]) for citation in citations}
        else:
            memories = {
                _endpoint(e["source"])
                for e in graph["edges"]
                if e["kind"] == "REFERENCES" and _endpoint(e["target"]) in selected
            }
        selected.update(memories & derived)
        result = {"rule": rule}
        for name, kind in (
            ("evidence", "EvidenceRevision"),
            ("runs", "Run"),
            ("cases", "CaseRevision"),
            ("memories", "MemorySnapshot"),
            ("revisions", "SkillRevision"),
        ):
            result[name] = [nodes[key] for key in sorted(selected) if key[0] == kind]
        result["links"] = [
            e
            for e in graph["edges"]
            if e["kind"] == "EVIDENCE_LINK" and _endpoint(e["source"]) == ("Rule", rule_key)
        ]
        return result

    def affected_by_memory(self, memory_key: str) -> dict:
        _require(_text(memory_key), "invalid_memory_identity")
        graph = self.inspect()
        nodes = {(n["kind"], n["key"]): n for n in graph["nodes"]}
        memories = {
            key
            for key, n in nodes.items()
            if n["kind"] == "MemorySnapshot"
            and (n["key"] == memory_key or n["payload"]["memory_id"] == memory_key)
        }
        _require(memories, "node_not_found")
        revisions, evidence, rules = set(), set(), set()
        for edge in graph["edges"]:
            left, right = _endpoint(edge["source"]), _endpoint(edge["target"])
            if edge["kind"] == "DERIVED_FROM" and right in memories:
                revisions.add(left)
            if edge["kind"] == "REFERENCES" and left in memories:
                evidence.add(right)
        owned_rules = {
            _endpoint(e["target"])
            for e in graph["edges"]
            if e["kind"] == "HAS_RULE" and _endpoint(e["source"]) in revisions
        }
        for identity in owned_rules:
            citations = nodes[identity]["payload"].get("memory_citations", [])
            if any(("MemorySnapshot", citation["target_id"]) in memories for citation in citations):
                rules.add(identity)
        for edge in graph["edges"]:
            if (
                edge["kind"] == "EVIDENCE_LINK"
                and _endpoint(edge["target"]) in evidence
                and _endpoint(edge["source"]) in owned_rules
            ):
                rules.add(_endpoint(edge["source"]))
        return {
            "memories": [nodes[key] for key in sorted(memories)],
            "revisions": [nodes[key] for key in sorted(revisions)],
            "rules": [nodes[key] for key in sorted(rules)],
        }
