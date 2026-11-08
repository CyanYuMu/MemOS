"""Opt-in tests against a dedicated Skill database, never the Memory database.

MEMOS_SKILL_GRAPH_TEST_CONFIG names a private JSON file containing the repository
connection fields. Test projects are unique and intentionally retained for audit.
"""

import json
import os
import threading
import uuid

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from test_graph import edge, node, provenance, revisions

from memos.skill_drafting.graph import EDGE_ENDPOINTS, NODE_KINDS, SkillGraphRepository


@pytest.fixture
def live_repositories():
    path = os.environ.get("MEMOS_SKILL_GRAPH_TEST_CONFIG")
    if not path:
        pytest.skip("dedicated Skill Graph configuration was not supplied")
    config = json.loads(Path(path).read_text())
    prefix = "graph-test-" + uuid.uuid4().hex
    opened = []

    def make(suffix="main", **overrides):
        parameters = {
            key: config[key]
            for key in ("uri", "user", "password", "database", "memory_database_id")
        }
        parameters.update(project_id=prefix + "-" + suffix, **overrides)
        repo = SkillGraphRepository(**parameters)
        opened.append(repo)
        return repo

    yield make
    for repo in opened:
        repo.close()


def test_live_all_types_trace_idempotency_isolation_and_reconnect(live_repositories):
    repo = live_repositories()
    identity = repo.initialize()["database_id"]
    nodes, edges = provenance()
    skill = next(n for n in nodes if n["kind"] == "Skill")
    current = next(n for n in nodes if n["kind"] == "SkillRevision")
    current["payload"]["purpose"] = "management_test"
    validation = next(n for n in nodes if n["kind"] == "Validation")
    older = node(
        "SkillRevision",
        skill_id="repair",
        revision="0",
        status="candidate",
        requires=[],
        purpose="management_test",
    )
    release = node("Release", release_id="release", purpose="management_test", authorized=False)
    other_skill = node("Skill", skill_id="other")
    other = node(
        "SkillRevision",
        skill_id="other",
        revision="1",
        status="candidate",
        requires=[],
        purpose="management_test",
    )
    nodes.extend([older, release, other_skill, other])
    edges.extend(
        [
            edge("HAS_REVISION", skill, older),
            edge("HAS_REVISION", other_skill, other),
            edge("RELATED_TO", skill, other_skill),
            edge("REQUIRES", current, other),
            edge("PUBLISHES", release, current),
            edge("BASED_ON", release, validation),
            edge("SUPERSEDES", current, older, scope="synthetic tests", approval_ref="release"),
        ]
    )
    assert {n["kind"] for n in nodes} == NODE_KINDS
    assert {e["kind"] for e in edges} == set(EDGE_ENDPOINTS)
    result = repo.store_bundle(nodes, edges)
    assert (result["node_count"], result["edge_count"]) == (12, 16)
    repeated = repo.store_bundle(nodes, edges)
    assert (repeated["nodes_added"], repeated["edges_added"]) == (0, 0)
    assert [n["key"] for n in repo.trace_rule("repair@1#R1")["memories"]] == ["snapshot"]
    assert [n["key"] for n in repo.affected_by_memory("memory")["rules"]] == ["repair@1#R1"]
    assert repo.neighbors("Skill", "other", relation="RELATED_TO")[0]["node"]["key"] == "repair"
    with pytest.raises(ValueError, match="graph_immutable_conflict"):
        repo.store_bundle([node("Skill", skill_id="repair", changed=True)], [])
    with pytest.raises(ValueError, match="graph_dangling_endpoint"):
        repo.store_bundle(
            [node("Skill", skill_id="uncommitted")],
            [
                edge(
                    "HAS_REVISION",
                    skill,
                    node(
                        "SkillRevision", skill_id="repair", revision="missing", status="candidate"
                    ),
                )
            ],
        )
    assert repo.get_node("Skill", "uncommitted") is None
    assert len(live_repositories().inspect()["nodes"]) == 12
    isolated = live_repositories("isolated")
    assert isolated.inspect()["nodes"] == []
    isolated.store_bundle([node("Skill", skill_id="repair", title="Separate project")], [])
    assert "title" not in repo.get_node("Skill", "repair")["payload"]
    forbidden = live_repositories("forbidden", memory_database_id=identity)
    with pytest.raises(ValueError, match="graph_memory_database_forbidden"):
        forbidden.initialize()


def test_live_concurrent_opposite_dependencies_commit_only_one(live_repositories):
    first, second = live_repositories("race"), live_repositories("race")
    a, b = revisions()
    first.store_bundle([a, b], [])
    second.initialize()
    barrier = threading.Barrier(2)

    def write(repo, left, right):
        barrier.wait(timeout=10)
        try:
            repo.store_bundle([], [edge("REQUIRES", left, right)])
            return "committed"
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        tasks = [executor.submit(write, first, a, b), executor.submit(write, second, b, a)]
        outcomes = [task.result(timeout=60) for task in tasks]
    assert sorted(outcomes) == ["committed", "graph_cycle"]
    graph = first.inspect()
    assert len(graph["nodes"]) == 2
    assert len(graph["edges"]) == 1
