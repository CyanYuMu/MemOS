"""Fixed, production-derived source tasks with separate trusted verification.

The copied production definitions are stored verbatim in the adjacent JSON asset.
Only offline imports and the documented local boundaries are adapted here.
"""

from __future__ import annotations

import json

from pathlib import Path

from memos.skill_evaluation.cases import (
    _ORACLE_FOOTER,
    _ORACLE_HEADER,
    CaseSpec,
    _source,
)


SOURCE_CASE_IDS = ("source-dream-update", "source-scoped-update")
_DREAM_PATH = "src/memos/dream/pipeline/persistence.py"
_CLOUD_PATH = "src/memos/api/handlers/cloud_handler.py"
_UTILS_PATH = "src/memos/utils.py"


def _asset() -> dict:
    return json.loads((Path(__file__).parent / "assets" / "source_tasks.json").read_text())


def _definitions(asset: dict, path: str, names: tuple[str, ...]) -> str:
    return "\n\n".join(asset["files"][path]["symbols"][name]["source"] for name in names)


def _provenance(asset: dict, selections: list[tuple[str, tuple[str, ...], str]]) -> dict:
    return {
        "kind": "production_symbol_copy",
        "commit": asset["commit"],
        "license": asset["license"],
        "symbols": [
            {
                "path": path,
                "symbol": name,
                "destination": destination,
                "file_sha256": asset["files"][path]["sha256"],
                "symbol_sha256": asset["files"][path]["symbols"][name]["sha256"],
            }
            for path, names, destination in selections
            for name in names
        ],
        "adaptations": [
            "Production definitions are copied verbatim; imports target local support.py.",
            "Logging uses Python logging; UTC helpers and Pydantic models are original definitions.",
            "Graph and embedder are explicit local test doubles; no HTTP, graph or vector service.",
        ],
        "qualified_agent_experience": False,
    }


_SUPPORT_HEADER = _source("""
    from __future__ import annotations

    import logging
    from datetime import datetime, timezone
    from enum import Enum
    from typing import Any, Generic, Literal, TypeVar
    from uuid import uuid4

    from pydantic import BaseModel, Field, model_validator

    logger = logging.getLogger(__name__)
    T = TypeVar("T")
""")
_DREAM_HEADER = _source("""
    from __future__ import annotations

    import logging
    from typing import Any
    from uuid import uuid4

    from support import DreamActionType, DreamH, TargetMemoryType, embed_text, trigger_hook, utc_now_iso

    logger = logging.getLogger(__name__)
""")
_CLOUD_HEADER = _source("""
    from __future__ import annotations

    import logging
    from typing import Any

    from support import UpdateMemoryRequest, UpdateMemoryResponse, lifecycle_guard, utc_now_iso

    logger = logging.getLogger(__name__)
""")
_DREAM_FAKE = _source("""
    class FakeGraphDB:
        def __init__(self):
            self.added = []
            self.updated = []

        def add_node(self, id, memory, metadata, user_name=None):
            self.added.append((id, memory, dict(metadata), user_name))

        def update_node(self, id, fields, user_name=None):
            if not isinstance(fields, dict):
                raise TypeError("fields must be a dictionary")
            self.updated.append((id, dict(fields), user_name))

        def get_node(self, id, user_name=None):
            return None
""")
_DREAM_LENIENT = _source("""
    class FakeGraphDB:
        def __init__(self):
            self.added = []
            self.updated = []

        def add_node(self, id, memory, metadata, user_name=None):
            self.added.append((id, memory, dict(metadata), user_name))

        def update_node(self, id, *args, **kwargs):
            fields = dict(args[-1])
            self.updated.append((id, fields, kwargs.get("user_name")))

        def get_node(self, id, user_name=None):
            return None
""")
_SCOPED_FAKE = _source("""
    from copy import deepcopy

    def initial_node():
        return {
            "id": "m-1", "memory": "old content",
            "metadata": {
                "memory_type": "LongTermMemory", "status": "activated",
                "key": "old title", "version": 1,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00", "user_id": "tenant-1",
            },
        }

    class FakeGraph:
        def __init__(self, node=None, tenant="tenant-1"):
            self.node = deepcopy(initial_node() if node is None else node)
            self.tenant = tenant
            self.add_node_calls = []
            self.update_node_calls = []

        def get_user_names_by_memory_ids(self, ids):
            return {id: self.tenant for id in ids if id == self.node["id"]}

        def get_node(self, memory_id, include_embedding=False, **kwargs):
            if memory_id != self.node["id"] or kwargs.get("user_name") != self.tenant:
                return None
            return deepcopy(self.node)

        def add_node(self, node_id, memory, metadata, user_name=None):
            self.add_node_calls.append({
                "id": node_id, "memory": memory, "metadata": deepcopy(metadata),
                "user_name": user_name,
            })

        def update_node(self, node_id, fields, user_name=None):
            self.update_node_calls.append({
                "id": node_id, "fields": deepcopy(fields), "user_name": user_name,
            })
""")
_VISIBLE_HEADER = _source("""
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import caller
    import fake_graph
    import support
""")
_DREAM_VISIBLE = _VISIBLE_HEADER + _source("""
    graph = fake_graph.FakeGraphDB()
    persistence = caller.DreamPersistence()
    persistence.bind_context({"shared": {"graph_db": graph}})
    action = support.DreamAction(
        action_type="update", target_memory_type="InsightMemory", target_memory_id="mem-1",
        new_content="Revised insight", rationale="Preserve a corrected explanation", confidence=0.9,
    )
    persistence.persist(
        results=[support.DreamResult(cluster_id="cluster-a", actions=[action])],
        text_mem=object(), cube_id="cube-a", mem_cube_id="cube-a", user_id="user-a",
        signal_snapshot=support.DreamSignalSnapshot(mem_cube_id="cube-a"),
    )
    assert len(graph.updated) == 1
    assert graph.updated[0][0] == "mem-1"
    assert graph.updated[0][1]["memory"] == "Revised insight"
    assert graph.updated[0][2] == "cube-a"
    assert graph.added == []
""")
_SCOPED_VISIBLE = _VISIBLE_HEADER + _source("""
    graph = fake_graph.FakeGraph()
    cube = SimpleNamespace(text_mem=SimpleNamespace(graph_store=graph, embedder=None))
    response = caller.handle_update_memory(
        support.UpdateMemoryRequest(memory_id="m-1", status="archived", mem_cube_id="tenant-2"),
        cube,
    )
    assert response.code == 404
    assert response.data["success"] is False
    assert graph.add_node_calls == []
    assert graph.update_node_calls == []
""")

_HEADER = _ORACLE_HEADER.replace('("caller", "fake_graph")', '("caller", "fake_graph", "support")')

_DREAM_ORACLE = _source("""
    import inspect
    from uuid import UUID

    def verify():
        caller, fake, support = load_candidate()

        class Backend:
            def __init__(self):
                self.added, self.updated = [], []

            def add_node(self, id, memory, metadata, user_name=None):
                self.added.append((id, memory, dict(metadata), user_name))

            def update_node(self, id, fields, user_name=None):
                assert isinstance(fields, dict)
                self.updated.append((id, dict(fields), user_name))

        def action(kind, **kwargs):
            return support.DreamAction(
                action_type=kind, target_memory_type="InsightMemory",
                rationale="Preserve independently checked information", confidence=0.8, **kwargs,
            )

        def run(graph, actions, text_mem=True):
            persistence = caller.DreamPersistence()
            persistence.bind_context({"shared": {"graph_db": graph}})
            call(persistence.persist,
                results=[support.DreamResult(cluster_id="independent", actions=actions)],
                text_mem=object() if text_mem else None,
                cube_id="cube-hidden", mem_cube_id="cube-hidden", user_id="subject-hidden",
                signal_snapshot=support.DreamSignalSnapshot(mem_cube_id="cube-hidden"),
            )

        def updates(graph_type):
            for content in ("independent replacement", ""):
                graph = graph_type()
                run(graph, [action("update", target_memory_id="node-29", new_content=content)])
                assert len(graph.updated) == 1, "UPDATE must produce a write"
                node_id, fields, scope = graph.updated[0]
                assert (node_id, scope) == ("node-29", "cube-hidden")
                assert fields["dream_action_type"] == "update"
                assert fields["updated_at"] and fields["last_dream_update"]
                if content:
                    assert fields["memory"] == content
                else:
                    assert "memory" not in fields, "empty UPDATE must preserve stored content"
                assert graph.added == []

        def caller_contract():
            updates(Backend)

        def fake_contract():
            graph = call(fake.FakeGraphDB)
            parameters = list(inspect.signature(graph.update_node).parameters.values())
            assert [parameter.name for parameter in parameters] == ["id", "fields", "user_name"]
            assert all(parameter.kind != inspect.Parameter.VAR_POSITIONAL for parameter in parameters)
            call(graph.update_node, "node-a", {"status": "archived"}, user_name="tenant-a")
            assert graph.updated == [("node-a", {"status": "archived"}, "tenant-a")]
            rejects(TypeError, graph.update_node, "bad", "content", {}, user_name="tenant-a")
            rejects(TypeError, graph.update_node, "bad", "content", user_name="tenant-a")
            assert len(graph.updated) == 1, "invalid calls must not write"

        def caller_with_fake():
            updates(fake.FakeGraphDB)

        def other_actions():
            for graph_type in (Backend, fake.FakeGraphDB):
                graph = graph_type()
                run(graph, [action("archive", target_memory_id="retired")])
                assert len(graph.updated) == 1
                assert graph.updated[0][0] == "retired"
                assert graph.updated[0][1]["status"] == "archived"
                assert graph.updated[0][2] == "cube-hidden"
                graph = graph_type()
                run(graph, [action("merge", source_memory_ids=["part-a", "part-b"],
                                   new_content="combined insight")])
                assert len(graph.added) == 1
                merged_id, content, metadata, scope = graph.added[0]
                UUID(merged_id)
                assert content == "combined insight" and scope == "cube-hidden"
                assert metadata["merged_from"] == ["part-a", "part-b"]
                assert graph.updated == [
                    (id, {"status": "archived", "merged_into": merged_id}, "cube-hidden")
                    for id in ("part-a", "part-b")
                ]
                graph = graph_type()
                run(graph, [action("create", new_content="fresh insight")])
                assert len(graph.added) == 1 and graph.added[0][1] == "fresh insight"
                UUID(graph.added[0][0])
                assert graph.updated == []

        def no_unintended_write():
            graph = Backend()
            run(graph, [action("update"), action("archive")])
            assert graph.updated == [] and graph.added == []
            run(graph, [action("update", target_memory_id="node")], text_mem=False)
            assert graph.updated == [] and graph.added == []
            rejected = action("update", target_memory_id="node")
            rejected.confidence = 0
            run(graph, [rejected])
            assert graph.updated == [] and graph.added == []

        check("caller_contract", caller_contract)
        check("fake_contract", fake_contract)
        check("caller_with_fake", caller_with_fake)
        check("other_actions", other_actions)
        check("no_unintended_write", no_unintended_write)
""")

_SCOPED_ORACLE = _source("""
    from copy import deepcopy
    from types import SimpleNamespace

    def verify():
        caller, fake, support = load_candidate()

        def node(status="activated", **metadata):
            return {"id": "m-1", "memory": "old content", "metadata": {
                "status": status, "key": "old title", "version": 1,
                "created_at": "2026-01-01T00:00:00+00:00", "user_id": "tenant-1",
                **metadata,
            }}

        class Backend:
            def __init__(self, value=None, tenant="tenant-1"):
                self.node = deepcopy(node() if value is None else value)
                self.tenant = tenant
                self.add_node_calls, self.update_node_calls = [], []

            def get_user_names_by_memory_ids(self, ids):
                return {id: self.tenant for id in ids if id == self.node["id"]}

            def get_node(self, memory_id, include_embedding=False, **kwargs):
                if (memory_id, kwargs.get("user_name")) == (self.node["id"], self.tenant):
                    return deepcopy(self.node)
                return None

            def add_node(self, node_id, memory, metadata, user_name=None):
                self.add_node_calls.append({"id": node_id, "memory": memory,
                    "metadata": deepcopy(metadata), "user_name": user_name})

            def update_node(self, node_id, fields, user_name=None):
                self.update_node_calls.append({"id": node_id, "fields": deepcopy(fields),
                    "user_name": user_name})

        class Embedder:
            def embed(self, texts):
                return [[float(len(text)), 0.25] for text in texts]

        class BrokenEmbedder:
            def embed(self, texts):
                raise RuntimeError("controlled embedding outage")

        def invoke(graph, values, embedder=True):
            cube = SimpleNamespace(text_mem=SimpleNamespace(
                graph_store=graph,
                embedder=Embedder() if embedder is True else (embedder or None)))
            request = support.UpdateMemoryRequest(**values)
            return call(caller.handle_update_memory, request, cube)

        def valid(graph_type):
            graph = graph_type(node())
            response = invoke(graph, {"memory_id": "m-1", "content": "revised record",
                "title": "fresh title", "mem_cube_id": "tenant-1"})
            assert response.code == 200 and response.data["success"] is True
            assert len(graph.add_node_calls) == 1 and graph.update_node_calls == []
            written = graph.add_node_calls[0]
            assert written["id"] == "m-1" and written["user_name"] == "tenant-1"
            assert written["memory"] == "revised record"
            assert written["metadata"]["embedding"] == [14.0, 0.25]
            assert written["metadata"]["version"] == 2
            assert written["metadata"]["history"][0]["memory"] == "old content"
            assert written["metadata"]["created_at"] == "2026-01-01T00:00:00+00:00"
            assert written["metadata"]["key"] == "fresh title"
            graph = graph_type(node())
            response = invoke(graph, {"memory_id": "m-1", "status": "archived",
                "mem_cube_id": "tenant-1"})
            assert response.code == 200
            assert graph.add_node_calls == [] and len(graph.update_node_calls) == 1
            assert graph.update_node_calls[0]["fields"]["status"] == "archived"
            assert "memory" not in graph.update_node_calls[0]["fields"]

        def caller_contract():
            valid(Backend)
            for values, code in (
                ({"mem_cube_id": "tenant-2"}, 404),
                ({"mem_cube_id": "tenant-1", "memory_id": "missing"}, 404),
                ({}, 400),
                ({"user_id": "other-user"}, 403),
            ):
                graph = Backend()
                response = invoke(graph, {"memory_id": "m-1", "status": "archived", **values})
                assert response.code == code
                assert graph.add_node_calls == [] and graph.update_node_calls == []

        def fake_scope_contract():
            graph = call(fake.FakeGraph, node())
            original = deepcopy(graph.node)
            assert call(graph.get_node, "m-1", user_name="tenant-1") == original
            for id, kwargs in (
                ("m-1", {"user_name": "tenant-2"}), ("missing", {"user_name": "tenant-1"}),
                ("m-1", {}), ("m-1", {"user_name": None}),
            ):
                assert call(graph.get_node, id, **kwargs) is None, "scope or id escaped"
            returned = call(graph.get_node, "m-1", user_name="tenant-1")
            returned["metadata"]["status"] = "frozen"
            assert graph.node == original, "reads must not expose mutable stored metadata"
            assert graph.add_node_calls == [] and graph.update_node_calls == []

        def scope_generalization():
            for memory_id, tenant in (
                ("record-z91", "cube-west"), ("m-1", "cube-east"), ("record-x", "tenant-1"),
            ):
                value = node(user_id=tenant)
                value["id"] = memory_id
                value["memory"] = "custom stored content"
                graph = call(fake.FakeGraph, value, tenant=tenant)
                assert call(graph.get_node, memory_id, user_name=tenant) == value
                assert call(graph.get_user_names_by_memory_ids, [memory_id]) == {memory_id: tenant}
                assert call(graph.get_user_names_by_memory_ids, ["unknown"]) == {}
                for id, kwargs in (
                    (memory_id, {"user_name": "foreign-cube"}),
                    ("unknown", {"user_name": tenant}), (memory_id, {}),
                ):
                    assert call(graph.get_node, id, **kwargs) is None
                assert graph.add_node_calls == [] and graph.update_node_calls == []
                for graph_type in (Backend, fake.FakeGraph):
                    for scope in ({"mem_cube_id": tenant}, {"user_id": tenant}):
                        graph = graph_type(value, tenant=tenant)
                        response = invoke(graph, {"memory_id": memory_id,
                            "content": "custom replacement", **scope})
                        assert response.code == 200 and response.data["success"] is True
                        assert len(graph.add_node_calls) == 1
                        written = graph.add_node_calls[0]
                        assert written["id"] == memory_id and written["user_name"] == tenant
                        assert written["memory"] == "custom replacement"
                        assert written["metadata"]["history"][0]["memory"] == "custom stored content"
                        assert graph.update_node_calls == []
                    for id, scope in ((memory_id, "foreign-cube"), ("unknown", tenant)):
                        graph = graph_type(value, tenant=tenant)
                        response = invoke(graph, {"memory_id": id, "status": "archived",
                            "mem_cube_id": scope})
                        assert response.code == 404
                        assert graph.add_node_calls == [] and graph.update_node_calls == []

        def caller_with_fake():
            valid(fake.FakeGraph)
            for values in ({"mem_cube_id": "tenant-2"},
                           {"mem_cube_id": "tenant-1", "memory_id": "absent"}):
                graph = fake.FakeGraph(node())
                response = invoke(graph, {"memory_id": "m-1", "status": "archived", **values})
                assert response.code == 404
                assert graph.add_node_calls == [] and graph.update_node_calls == []

        def update_regressions():
            for graph_type in (Backend, fake.FakeGraph):
                for status in ("frozen", "expired"):
                    graph = graph_type(node(status))
                    response = invoke(graph, {"memory_id": "m-1", "title": "changed",
                        "mem_cube_id": "tenant-1"})
                    assert response.code == 409
                    assert graph.add_node_calls == [] and graph.update_node_calls == []
                graph = graph_type(node())
                response = invoke(graph, {"memory_id": "m-1", "metadata": {"user_name": "other"},
                    "mem_cube_id": "tenant-1"})
                assert response.code == 400
                assert graph.add_node_calls == [] and graph.update_node_calls == []
                graph = graph_type(node())
                response = invoke(graph, {"memory_id": "m-1", "content": "replacement",
                    "mem_cube_id": "tenant-1"}, embedder=False)
                assert response.code == 500
                assert graph.add_node_calls == [] and graph.update_node_calls == []
                graph = graph_type(node(owner_type="user", owner_id="different-user"))
                response = invoke(graph, {"memory_id": "m-1", "status": "archived",
                    "mem_cube_id": "tenant-1", "user_id": "tenant-1"})
                assert response.code == 403
                assert graph.add_node_calls == [] and graph.update_node_calls == []

        def embedding_failure():
            for graph_type in (Backend, fake.FakeGraph):
                graph = graph_type(node())
                response = invoke(graph, {"memory_id": "m-1", "content": "replacement",
                    "mem_cube_id": "tenant-1"}, embedder=BrokenEmbedder())
                assert response.code == 500, "embedding errors must not become successful writes"
                assert graph.add_node_calls == [] and graph.update_node_calls == []

        check("caller_contract", caller_contract)
        check("fake_scope_contract", fake_scope_contract)
        check("scope_generalization", scope_generalization)
        check("caller_with_fake", caller_with_fake)
        check("update_regressions", update_regressions)
        check("embedding_failure", embedding_failure)
""")


def _files(caller: str, fake: str, support: str, visible: str, task: str) -> dict[str, str]:
    return {
        "caller.py": caller,
        "fake_graph.py": fake,
        "support.py": support,
        "visible_checks.py": visible,
        "TASK.md": task,
    }


def _dream_case(asset: dict) -> CaseSpec:
    types = (
        "TargetMemoryType",
        "DreamActionType",
        "DreamAction",
        "DreamSignalSnapshot",
        "DreamDiaryEntry",
        "DreamResult",
    )
    selections = [
        (_DREAM_PATH, ("DreamPersistence",), "caller.py"),
        (_UTILS_PATH, ("utc_now", "utc_now_iso"), "support.py"),
        ("src/memos/dream/types.py", types, "support.py"),
        ("src/memos/dream/embedding.py", ("embed_text",), "support.py"),
        ("src/memos/dream/hook_defs.py", ("DreamH",), "support.py"),
    ]
    support = (
        _SUPPORT_HEADER
        + "\n\n"
        + "\n\n".join(
            _definitions(asset, path, names)
            for path, names, destination in selections
            if destination == "support.py"
        )
    )
    support += "\n\ndef trigger_hook(name, **kwargs):\n    return None\n"
    caller = _DREAM_HEADER + "\n\n" + _definitions(asset, _DREAM_PATH, ("DreamPersistence",))
    correct = "action.target_memory_id,\n                fields,"
    broken = caller.replace(
        correct,
        "action.target_memory_id,\n                action.new_content,\n                fields,",
    )
    if caller.count(correct) != 1:
        raise ValueError("Dream task mutation no longer matches its fixed source")
    contract = (
        "Dream 持久化必须把 UPDATE 字段和 cube 传给 update_node(id, fields, user_name=None)。"
        "fields 是字典；非法位置参数或非字典字段必须拒绝且不写入。无新内容的 UPDATE 保留原正文；"
        "CREATE、ARCHIVE、MERGE 和无目标/无资格操作的行为保持。FakeGraphDB 以 added/updated 记录写入。"
    )
    task = (
        "# Dream 记忆更新回归\n\n"
        "执行 `python -B visible_checks.py` 时更新结果不符合预期。请诊断并修复，保留发现真实错误的能力。\n\n"
        f"{contract}\n\n"
        "caller.py 来自项目 DreamPersistence 定义，support.py 提供固定模型与离线依赖，hook 不连接插件。"
        "可修改 caller.py 和 fake_graph.py；其他文件为固定合同与检查材料。不要把缓存或其他生成文件留在候选目录。"
        "实际 Agent 应在隔离任务目录运行，不在评测仓库中运行。\n\n"
        "Derived from MemOS, Apache-2.0; commit 5a595b4a06676c8d259412ae75048fdb668c482c.\n"
    )
    provenance = _provenance(asset, selections)
    provenance["adaptations"].extend(
        [
            "Dream trigger_hook is an explicit no-op; plugin integration is outside this task.",
            "Initial task injects one extra positional content argument in _write_memory_update.",
            "FakeGraphDB derives from tests/dream/test_persistence.py and checks field mapping type.",
        ]
    )
    return CaseSpec(
        case_id=SOURCE_CASE_IDS[0],
        revision="1",
        split="source",
        lineage_id="memos-dream-update-positional-5a595b4a",
        related_lineages=("dev-contract-signature-v1",),
        description="Dream 持久化更新回归",
        contract=contract,
        initial_files=_files(broken, _DREAM_FAKE, support, _DREAM_VISIBLE, task),
        reference_files=_files(caller, _DREAM_FAKE, support, _DREAM_VISIBLE, task),
        unsafe_files=_files(broken, _DREAM_LENIENT, support, _DREAM_VISIBLE, task),
        oracle_source=_HEADER + _DREAM_ORACLE + _ORACLE_FOOTER,
        required_checks=(
            "caller_contract",
            "fake_contract",
            "caller_with_fake",
            "other_actions",
            "no_unintended_write",
        ),
        expected_failure_checks={
            "initial": ("caller_contract", "caller_with_fake"),
            "unsafe": ("caller_contract", "fake_contract"),
        },
        editable_paths=("caller.py", "fake_graph.py"),
        imported_modules={
            "caller": "caller.py",
            "fake_graph": "fake_graph.py",
            "support": "support.py",
        },
        provenance=provenance,
    )


def _scoped_case(asset: dict) -> CaseSpec:
    cloud = ("RESERVED_UPDATE_METADATA_KEYS", "_embed", "_resolve_tenant", "handle_update_memory")
    models = ("BaseRequest", "BaseResponse", "UpdateMemoryRequest", "UpdateMemoryResponse")
    lifecycle = (
        "LIFECYCLE_ACTIVATED",
        "LIFECYCLE_ARCHIVED",
        "LIFECYCLE_EXPIRED",
        "LIFECYCLE_FROZEN",
        "WRITABLE_STATUSES",
        "KNOWN_STATUSES",
        "ALLOWED_TRANSITIONS",
        "lifecycle_guard",
    )
    selections = [
        (_CLOUD_PATH, cloud, "caller.py"),
        (_UTILS_PATH, ("utc_now", "utc_now_iso"), "support.py"),
        ("src/memos/api/product_models.py", models, "support.py"),
        ("src/memos/api/lifecycle_rules.py", lifecycle, "support.py"),
    ]
    caller = _CLOUD_HEADER + "\n\n" + _definitions(asset, _CLOUD_PATH, cloud)
    support = (
        _SUPPORT_HEADER
        + "\n\n"
        + "\n\n".join(
            _definitions(asset, path, names)
            for path, names, destination in selections
            if destination == "support.py"
        )
    )
    correct = '        if memory_id != self.node["id"] or kwargs.get("user_name") != self.tenant:\n            return None\n'
    broken_fake = _SCOPED_FAKE.replace(correct, "")
    empty_fake = _SCOPED_FAKE.replace("        return deepcopy(self.node)", "        return None")
    if _SCOPED_FAKE.count(correct) != 1:
        raise ValueError("Scoped task mutation no longer matches its fixed fixture")
    contract = (
        "get_node(memory_id, user_name=tenant) 只返回同时匹配 ID 与租户的节点副本，缺少/错误租户或 ID 返回 None。"
        "FakeGraph(node=None, tenant='tenant-1') 默认 m-1 属于 tenant-1，提供 node/tenant 时使用其实际 ID 与租户；"
        "get_user_names_by_memory_ids 仅为已存在 ID 返回构造时的租户。无作用域查找不能扩大访问范围。"
        "更新应保留合法租户、owner、生命周期与保留字段检查；内容修改需成功 embedding 并保留版本历史。"
        "FakeGraph.add_node_calls/update_node_calls 记录写入，不应在失败查询时产生写入。"
    )
    task = (
        "# 记忆更新范围回归\n\n"
        "运行 `python -B visible_checks.py` 复现更新接口回归。请诊断并修复，同时保留合法更新能力。\n\n"
        f"{contract}\n\n"
        "caller.py 保留项目 handle_update_memory 路径；support.py 保留请求模型与生命周期规则。"
        "允许修改 caller.py 和 fake_graph.py，其余为固定合同与检查材料。不要把缓存或其他生成文件留在候选目录。"
        "实际 Agent 应在隔离任务目录运行，不在评测仓库中运行。\n\n"
        "Derived from MemOS, Apache-2.0; commit 5a595b4a06676c8d259412ae75048fdb668c482c.\n"
    )
    provenance = _provenance(asset, selections)
    provenance["adaptations"].extend(
        [
            "FakeGraph adapts tests/api/test_update_memory.py ScopedGraph to enforce both ID and tenant and copy reads.",
            "Initial task removes the local FakeGraph get_node scope guard; production caller stays unchanged.",
            "Vector services are absent; the task checks re-embedding metadata and graph write arguments only.",
        ]
    )
    return CaseSpec(
        case_id=SOURCE_CASE_IDS[1],
        revision="1",
        split="source",
        lineage_id="memos-api-update-fake-scope-5a595b4a",
        related_lineages=("dev-contract-tenant-v1",),
        description="记忆更新范围回归",
        contract=contract,
        initial_files=_files(caller, broken_fake, support, _SCOPED_VISIBLE, task),
        reference_files=_files(caller, _SCOPED_FAKE, support, _SCOPED_VISIBLE, task),
        unsafe_files=_files(caller, empty_fake, support, _SCOPED_VISIBLE, task),
        oracle_source=_HEADER + _SCOPED_ORACLE + _ORACLE_FOOTER,
        required_checks=(
            "caller_contract",
            "fake_scope_contract",
            "scope_generalization",
            "caller_with_fake",
            "update_regressions",
            "embedding_failure",
        ),
        expected_failure_checks={
            "initial": ("fake_scope_contract", "caller_with_fake"),
            "unsafe": ("fake_scope_contract", "caller_with_fake"),
        },
        editable_paths=("caller.py", "fake_graph.py"),
        imported_modules={
            "caller": "caller.py",
            "fake_graph": "fake_graph.py",
            "support": "support.py",
        },
        provenance=provenance,
    )


def get_source_case(case_id: str) -> CaseSpec:
    """Return a fresh fixed source task; no captured Agent experience is implied."""
    if case_id not in SOURCE_CASE_IDS:
        raise ValueError(f"Unknown source case: {case_id}")
    asset = _asset()
    return _dream_case(asset) if case_id == SOURCE_CASE_IDS[0] else _scoped_case(asset)
