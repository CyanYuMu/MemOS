"""Graph contract tests use a transactional driver double, never Memory storage."""

import builtins
import copy
import json
import threading

from concurrent.futures import ThreadPoolExecutor

import pytest

from memos.skill_drafting.graph import (
    SkillGraphRepository,
    edge_key,
    node_key,
)


class Result(list):
    def single(self, strict=False):
        return self[0] if self else None

    def consume(self):
        return None


class Driver:
    def __init__(self, database_id="skill-database"):
        self.database_id = database_id
        self.nodes = {}
        self.edges = {}
        self.lock = threading.RLock()
        self.calls = []
        self.fail_write = False

    def session(self, **kwargs):
        return Session(self)

    def close(self):
        pass


class Session:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query, **params):
        self.driver.calls.append(query)
        if "sg:database" in query:
            return Result([{"id": self.driver.database_id}])
        if query.startswith("CREATE CONSTRAINT"):
            return Result()
        raise AssertionError(query)

    def begin_transaction(self, **kwargs):
        return Transaction(self.driver)


class Transaction:
    def __init__(self, driver):
        self.driver = driver
        self.locked = False

    def __enter__(self):
        self.driver.lock.acquire()
        self.locked = True
        self.nodes = copy.deepcopy(self.driver.nodes)
        self.edges = copy.deepcopy(self.driver.edges)
        return self

    def __exit__(self, *args):
        if self.locked:
            self.driver.lock.release()
            self.locked = False
        return False

    def run(self, query, **params):
        self.driver.calls.append(query)
        if "sg:lock" in query:
            return Result([{"sequence": 1}])
        if "sg:nodes" in query:
            return Result(
                [{"n": n} for n in self.nodes.values() if n["project_id"] == params["project_id"]]
            )
        if "sg:edges" in query:
            return Result(
                [
                    {
                        "r": e,
                        "source": self.nodes[e["source_uid"]],
                        "target": self.nodes[e["target_uid"]],
                    }
                    for e in self.edges.values()
                    if e["project_id"] == params["project_id"]
                    or self.nodes[e["source_uid"]]["project_id"] == params["project_id"]
                    or self.nodes[e["target_uid"]]["project_id"] == params["project_id"]
                ]
            )
        if "sg:put-node" in query:
            self.nodes[params["record"]["uid"]] = copy.deepcopy(params["record"])
            return Result()
        if "sg:put-edge" in query:
            if self.driver.fail_write:
                raise RuntimeError("private-driver-endpoint")
            self.edges[params["record"]["uid"]] = copy.deepcopy(params["record"])
            return Result()
        raise AssertionError(query)

    def commit(self):
        self.driver.nodes, self.driver.edges = self.nodes, self.edges


def node(node_type, **payload):
    return {"kind": node_type, "key": node_key(node_type, payload), "payload": payload}


def edge(edge_type, source, target, **payload):
    left = {key: source[key] for key in ("kind", "key")}
    right = {key: target[key] for key in ("kind", "key")}
    return {
        "kind": edge_type,
        "key": edge_key(edge_type, left, right, payload),
        "source": left,
        "target": right,
        "payload": payload,
    }


def repository(driver=None, project="example", memory_database_id="memory-database"):
    return SkillGraphRepository(
        "bolt://synthetic",
        user="synthetic",
        password="synthetic",
        project_id=project,
        memory_database_id=memory_database_id,
        driver=driver or Driver(),
    )


def revisions():
    a = node(
        "SkillRevision",
        skill_id="a",
        revision="1",
        status="candidate",
        purpose="management_test",
        requires=[],
    )
    b = node(
        "SkillRevision",
        skill_id="b",
        revision="1",
        status="candidate",
        purpose="management_test",
        requires=[],
    )
    return a, b


def provenance():
    skill = node("Skill", skill_id="repair")
    revision = node(
        "SkillRevision", skill_id="repair", revision="1", status="candidate", requires=[]
    )
    rule = node(
        "Rule",
        skill_id="repair",
        revision="1",
        rule_id="R1",
        memory_citations=[{"target_kind": "MemorySnapshot", "target_id": "snapshot"}],
    )
    memory = node(
        "MemorySnapshot",
        snapshot_id="snapshot",
        memory_id="memory",
        content_sha256="content",
        evidence_revision="evidence",
    )
    evidence = node(
        "EvidenceRevision",
        evidence_revision="evidence",
        uri="file:///synthetic/evidence.json",
        sha256="evidence",
        run_id="run",
        attempt_id="attempt",
        case_id="case",
        case_revision="1",
    )
    run = node("Run", run_id="run", case_id="case", case_revision="1", attempt_id="attempt")
    case = node(
        "CaseRevision", case_id="case", case_revision="1", lineage_id="lineage", split="source"
    )
    validation = node(
        "Validation",
        validation_id="validation",
        kind="structural",
        status="passed",
        semantic_review="pending",
    )
    nodes = [skill, revision, rule, memory, evidence, run, case, validation]
    edges = [
        edge("HAS_REVISION", skill, revision),
        edge("HAS_RULE", revision, rule),
        edge("DERIVED_FROM", revision, memory),
        edge("REFERENCES", memory, evidence),
        edge(
            "EVIDENCE_LINK",
            rule,
            evidence,
            link_id="link",
            kind="supports",
            locator="/case/contract",
            claim="Observed contract",
            producer="synthetic",
            semantic_review="pending",
        ),
        edge("FROM_RUN", evidence, run),
        edge("OF_CASE", run, case),
        edge("EVALUATES", validation, revision),
        edge("USES_RUN", validation, run),
    ]
    return nodes, edges


def test_source_history_cannot_gain_another_case_revision():
    repo = repository()
    nodes, edges = provenance()
    repo.store_bundle(nodes, edges)
    before = repo.inspect()
    run = next(n for n in nodes if n["kind"] == "Run")
    other = node(
        "CaseRevision", case_id="case", case_revision="2", lineage_id="lineage", split="source"
    )
    with pytest.raises(ValueError, match="graph_ownership_mismatch"):
        repo.store_bundle([other], [edge("OF_CASE", run, other)])
    assert repo.inspect() == before


def test_evidence_cannot_omit_run_binding_or_gain_another_run():
    nodes, edges = provenance()
    evidence = next(n for n in nodes if n["kind"] == "EvidenceRevision")
    del evidence["payload"]["run_id"]
    with pytest.raises(ValueError, match="graph_(node_fields_missing|ownership_mismatch)"):
        repository().store_bundle(nodes, edges)
    evidence["payload"]["run_id"] = "run"
    repo = repository()
    repo.store_bundle(nodes, edges)
    before = repo.inspect()
    other = node("Run", run_id="other-run", case_id="case", case_revision="1", attempt_id="other")
    with pytest.raises(ValueError, match="graph_ownership_mismatch"):
        repo.store_bundle([other], [edge("FROM_RUN", evidence, other)])
    assert repo.inspect() == before


@pytest.mark.parametrize("field", ["target_kind", "target_id", "skill_id", "revision", "rule_id"])
def test_evidence_link_identity_must_match_its_endpoints(field):
    nodes, edges = provenance()
    link = next(e for e in edges if e["kind"] == "EVIDENCE_LINK")
    link["payload"][field] = "different"
    with pytest.raises(ValueError, match="graph_ownership_mismatch"):
        repository().store_bundle(nodes, edges)


def test_cannot_append_evidence_claim_not_declared_in_frozen_rule():
    nodes, edges = provenance()
    link = next(e for e in edges if e["kind"] == "EVIDENCE_LINK")
    rule = next(n for n in nodes if n["kind"] == "Rule")
    rule["payload"]["rule"] = {"evidence_links": [copy.deepcopy(link["payload"])]}
    repo = repository()
    repo.store_bundle(nodes, edges)
    extra = copy.deepcopy(link)
    extra["key"] = extra["payload"]["link_id"] = "new-claim"
    extra["payload"]["claim"] = "unfrozen claim"
    with pytest.raises(ValueError, match="graph_evidence_link_undeclared"):
        repo.store_bundle([], [extra])


def test_memory_database_is_rejected_before_any_constraint_or_write():
    driver = Driver("memory-database")
    with pytest.raises(ValueError, match="graph_memory_database_forbidden"):
        repository(driver).initialize()
    assert not any(q.startswith("CREATE") for q in driver.calls)
    assert not driver.nodes


def test_unknown_or_changed_database_identity_fails_closed():
    driver = Driver(None)
    with pytest.raises(ValueError, match="graph_database_unverified"):
        repository(driver).initialize()
    driver.database_id = "first"
    repo = repository(driver)
    repo.initialize()
    driver.database_id = "replacement"
    with pytest.raises(ValueError, match="graph_database_changed"):
        repo.inspect()


def test_store_is_idempotent_and_rejects_overwriting_a_logical_identity():
    repo = repository()
    skill = node("Skill", skill_id="repair", title="Repair")
    revision = node(
        "SkillRevision", skill_id="repair", revision="1", status="candidate", requires=[]
    )
    relation = edge("HAS_REVISION", skill, revision)
    result = repo.store_bundle([skill, revision], [relation])
    assert (result["nodes_added"], result["edges_added"]) == (2, 1)
    assert repo.store_bundle([skill, revision], [relation])["nodes_added"] == 0
    changed = copy.deepcopy(skill)
    changed["payload"]["title"] = "Different content"
    with pytest.raises(ValueError, match="graph_immutable_conflict"):
        repo.store_bundle([changed], [])
    assert repo.get_node("Skill", "repair") == skill
    assert repo.get_node("Skill", "missing") is None


def test_scope_and_logical_identity_cannot_be_supplied_by_payload():
    repo = repository()
    skill = node("Skill", skill_id="repair")
    skill["key"] = "duplicate-key"
    with pytest.raises(ValueError, match="graph_identity_mismatch"):
        repo.store_bundle([skill], [])
    skill = node("Skill", skill_id="repair", project_id="foreign")
    with pytest.raises(ValueError, match="graph_project_mismatch"):
        repo.store_bundle([skill], [])
    skill = node("Skill", skill_id="repair")
    skill["project_id"] = "foreign"
    with pytest.raises(ValueError, match="graph_invalid_node"):
        repo.store_bundle([skill], [])


@pytest.mark.parametrize("kind", ["DROP", "Memory", "Skill` DETACH DELETE n //"])
def test_unapproved_node_type_never_reaches_cypher(kind):
    driver = Driver()
    with pytest.raises(ValueError):
        repository(driver).store_bundle([{"kind": kind, "key": "x", "payload": {}}], [])
    assert not driver.nodes


def test_missing_or_wrong_endpoints_rollback_entire_bundle():
    repo = repository()
    a, b = revisions()
    relation = edge("REQUIRES", a, b)
    with pytest.raises(ValueError, match="graph_dangling_endpoint"):
        repo.store_bundle([a], [relation])
    assert repo.inspect()["nodes"] == []
    relation["kind"] = "HAS_RULE"
    relation["key"] = edge_key("HAS_RULE", relation["source"], relation["target"], {})
    with pytest.raises(ValueError, match="graph_endpoint_type"):
        repo.store_bundle([a, b], [relation])
    assert repo.inspect()["nodes"] == []


def test_cycle_and_self_reference_cannot_commit():
    repo = repository()
    a, b = revisions()
    with pytest.raises(ValueError, match="graph_cycle"):
        repo.store_bundle([a, b], [edge("REQUIRES", a, b), edge("REQUIRES", b, a)])
    assert repo.inspect()["nodes"] == []
    with pytest.raises(ValueError, match="graph_self_reference"):
        repo.store_bundle([a], [edge("REQUIRES", a, a)])


def test_concurrent_opposite_dependencies_never_both_commit():
    driver = Driver()
    first, second = repository(driver), repository(driver)
    a, b = revisions()
    first.store_bundle([a, b], [])
    second.initialize()

    def add(repo, start, end):
        try:
            repo.store_bundle([], [edge("REQUIRES", start, end)])
            return "stored"
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda args: add(*args), [(first, a, b), (second, b, a)]))
    assert sorted(outcomes) == ["graph_cycle", "stored"]
    assert len(first.inspect()["edges"]) == 1


def test_real_revision_dependency_must_have_been_declared():
    repo = repository()
    a = node("SkillRevision", skill_id="a", revision="1", status="candidate", requires=[])
    b = node("SkillRevision", skill_id="b", revision="1", status="candidate", requires=[])
    with pytest.raises(ValueError, match="graph_dependency_undeclared"):
        repo.store_bundle([a, b], [edge("REQUIRES", a, b)])


def test_database_failure_rolls_back_and_hides_driver_details():
    driver = Driver()
    repo = repository(driver)
    a, b = revisions()
    driver.fail_write = True
    with pytest.raises(RuntimeError, match="^graph_unavailable$"):
        repo.store_bundle([a, b], [edge("REQUIRES", a, b)])
    assert driver.nodes == {} and driver.edges == {}


def test_related_to_is_symmetric_but_not_transitive():
    repo = repository()
    a, b, c = [node("Skill", skill_id=name) for name in ("a", "b", "c")]
    repo.store_bundle([a, b, c], [edge("RELATED_TO", a, b), edge("RELATED_TO", b, c)])
    assert {x["node"]["key"] for x in repo.neighbors("Skill", "b", relation="RELATED_TO")} == {
        "a",
        "c",
    }
    assert {x["node"]["key"] for x in repo.neighbors("Skill", "a", relation="RELATED_TO")} == {"b"}
    assert repo.store_bundle([], [edge("RELATED_TO", b, a)])["edges_added"] == 0


def test_projects_cannot_reference_one_anothers_nodes():
    driver = Driver()
    first, second = repository(driver, "first"), repository(driver, "second")
    a, b = revisions()
    first.store_bundle([a], [])
    second.store_bundle([b], [])
    with pytest.raises(ValueError, match="graph_dangling_endpoint"):
        first.store_bundle([], [edge("REQUIRES", a, b)])
    assert [n["key"] for n in first.inspect()["nodes"]] == [a["key"]]


def test_synthetic_release_is_explicitly_not_authorization():
    repo = repository()
    release = node(
        "Release", release_id="test-release", purpose="management_test", authorized=False
    )
    repo.store_bundle([release], [])
    for changed in (
        {**release["payload"], "authorized": True},
        {**release["payload"], "purpose": "release"},
    ):
        with pytest.raises(ValueError, match="graph_release_not_supported"):
            repo.store_bundle([node("Release", **changed)], [])


def test_importing_graph_does_not_require_the_neo4j_optional_package(monkeypatch):
    original = builtins.__import__

    def no_neo4j(name, *args, **kwargs):
        if name == "neo4j":
            raise ImportError("synthetic missing optional dependency")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_neo4j)
    assert repository().inspect()["nodes"] == []
    with pytest.raises(ImportError, match="neo4j"):
        SkillGraphRepository(
            "bolt://synthetic", user="u", password="p", project_id="p", memory_database_id="memory"
        )


def test_rule_trace_and_memory_reverse_lookup_return_exact_provenance():
    repo = repository()
    nodes, edges = provenance()
    repo.store_bundle(nodes, edges)
    trace = repo.trace_rule("repair@1#R1")
    assert [n["key"] for n in trace["evidence"]] == ["evidence"]
    assert [n["key"] for n in trace["runs"]] == ["run"]
    assert [n["key"] for n in trace["cases"]] == ["case@1"]
    assert [n["key"] for n in trace["memories"]] == ["snapshot"]
    result = repo.affected_by_memory("memory")
    assert [n["key"] for n in result["revisions"]] == ["repair@1"]
    assert [n["key"] for n in result["rules"]] == ["repair@1#R1"]


def test_shared_evidence_does_not_mix_other_revisions_or_memory_observations():
    repo = repository()
    nodes, edges = provenance()
    other = node("SkillRevision", skill_id="other", revision="1", status="candidate", requires=[])
    rule = node(
        "Rule",
        skill_id="other",
        revision="1",
        rule_id="R2",
        memory_citations=[{"target_kind": "MemorySnapshot", "target_id": "other-snapshot"}],
    )
    memory = node(
        "MemorySnapshot",
        snapshot_id="other-snapshot",
        memory_id="other-memory",
        content_sha256="other",
        evidence_revision="evidence",
    )
    evidence = next(n for n in nodes if n["kind"] == "EvidenceRevision")
    additions = [
        edge("HAS_RULE", other, rule),
        edge("DERIVED_FROM", other, memory),
        edge("REFERENCES", memory, evidence),
        edge(
            "EVIDENCE_LINK",
            rule,
            evidence,
            link_id="link-other",
            kind="supports",
            locator="/case/contract",
            claim="Same contract",
            producer="synthetic",
            semantic_review="pending",
        ),
    ]
    repo.store_bundle([*nodes, other, rule, memory], edges + additions)
    assert [n["key"] for n in repo.trace_rule("repair@1#R1")["memories"]] == ["snapshot"]
    assert [n["key"] for n in repo.affected_by_memory("memory")["rules"]] == ["repair@1#R1"]


def test_existing_evidence_link_cannot_change_claim_or_target():
    repo = repository()
    nodes, edges = provenance()
    repo.store_bundle(nodes, edges)
    changed = copy.deepcopy(next(e for e in edges if e["kind"] == "EVIDENCE_LINK"))
    changed["payload"]["claim"] = "A different claim"
    with pytest.raises(ValueError, match="graph_immutable_conflict"):
        repo.store_bundle([], [changed])


def test_nine_node_types_and_approved_synthetic_replacement():
    repo = repository()
    nodes, edges = provenance()
    current = next(n for n in nodes if n["kind"] == "SkillRevision")
    current["payload"]["purpose"] = "management_test"
    older = node(
        "SkillRevision",
        skill_id="repair",
        revision="0",
        status="candidate",
        requires=[],
        purpose="management_test",
    )
    release = node("Release", release_id="release", purpose="management_test", authorized=False)
    validation = next(n for n in nodes if n["kind"] == "Validation")
    replacement = edge(
        "SUPERSEDES", current, older, scope="synthetic tasks", approval_ref="release"
    )
    with pytest.raises(ValueError, match="graph_replacement_approval_mismatch"):
        repo.store_bundle([*nodes, older, release], [*edges, replacement])
    result = repo.store_bundle(
        [*nodes, older, release],
        [
            *edges,
            edge("PUBLISHES", release, current),
            edge("BASED_ON", release, validation),
            replacement,
        ],
    )
    assert result["node_count"] == 10
    assert len({n["kind"] for n in repo.inspect()["nodes"]}) == 9


def test_readback_detects_mutated_stored_payload():
    driver = Driver()
    repo = repository(driver)
    repo.store_bundle([node("Skill", skill_id="repair")], [])
    next(iter(driver.nodes.values()))["payload_json"] = json.dumps(
        {"skill_id": "repair", "changed": True}
    )
    with pytest.raises(ValueError, match="graph_stored_content_mismatch"):
        repo.inspect()


def test_scope_check_includes_intermediate_edge_endpoints():
    driver = Driver()
    first, second = repository(driver, "first"), repository(driver, "second")
    a, b = revisions()
    first.store_bundle([a], [])
    second.store_bundle([b], [])
    relation = edge("REQUIRES", a, b)
    raw = first._record("edge", relation)
    raw["target_uid"] = second._record("node", b)["uid"]
    driver.edges[raw["uid"]] = raw
    with pytest.raises(ValueError, match="graph_project_mismatch"):
        first.inspect()


def test_graph_size_limit_is_explicit_not_silent_truncation():
    repo = repository()
    repo.max_nodes = 1
    with pytest.raises(ValueError, match="graph_size_limit"):
        repo.store_bundle([node("Skill", skill_id="a"), node("Skill", skill_id="b")], [])


@pytest.mark.parametrize("kind", [[], {}, None])
def test_invalid_kind_types_are_rejected_with_stable_errors(kind):
    repo = repository()
    with pytest.raises(ValueError, match="graph_invalid_node"):
        repo.store_bundle([{"kind": kind, "key": "x", "payload": {}}], [])
    with pytest.raises(ValueError, match="graph_invalid_edge"):
        repo.store_bundle(
            [], [{"kind": kind, "key": "x", "source": {}, "target": {}, "payload": {}}]
        )


@pytest.mark.parametrize("citations", [None, {}, ["snapshot"], [{"target_id": "snapshot"}]])
def test_rule_memory_citations_have_explicit_snapshot_identity(citations):
    nodes, edges = provenance()
    rule = next(n for n in nodes if n["kind"] == "Rule")
    rule["payload"]["memory_citations"] = citations
    with pytest.raises(ValueError, match="graph_invalid_memory_citation"):
        repository().store_bundle(nodes, edges)


def test_memory_citation_must_belong_to_rule_revision_sources():
    nodes, edges = provenance()
    rule = next(n for n in nodes if n["kind"] == "Rule")
    rule["payload"]["memory_citations"][0]["target_id"] = "missing"
    with pytest.raises(ValueError, match="graph_memory_citation_unbound"):
        repository().store_bundle(nodes, edges)
