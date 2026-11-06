"""In-memory stand-in for the graph store plus its vector payload.

It follows the two value rules that matter for the version chain: Neo4j rejects
map properties (so lists of dicts must already be JSON strings when they reach
``update_node``), and ``update_node`` mirrors the written fields into the Qdrant
payload (``Neo4jGraphDB._sync_vector_payload``). ``vec_db`` is ``None`` so the
handler's explicit payload sync is skipped here.
"""

from __future__ import annotations

import json

from typing import Any

from memos.memories.textual.item import TextualMemoryItem


def _check_neo4j_value(key: str, value: Any) -> None:
    if value is None or isinstance(value, str | int | float | bool):
        return
    if isinstance(value, list) and all(isinstance(v, str | int | float | bool) for v in value):
        return
    raise TypeError(f"Property values can only be of primitive types or arrays thereof: {key}")


class FakeGraph:
    vec_db = None

    def __init__(self):
        self.nodes: dict[str, dict[str, Any]] = {}
        self.payloads: dict[str, dict[str, Any]] = {}
        self.edges: list[tuple[str, str, str]] = []
        self.deleted: list[str] = []
        self.neighbors: dict[tuple[float, ...], list[str]] = {}

    # ---- seeding ----
    def seed(self, item: TextualMemoryItem, neighbors: list[str] | None = None) -> str:
        dumped = item.metadata.model_dump(exclude_none=True)
        self.add_node(item.id, item.memory, dumped)
        if neighbors is not None:
            self.neighbors[tuple(item.metadata.embedding)] = neighbors
        return item.id

    def item(self, node_id: str) -> TextualMemoryItem:
        return TextualMemoryItem.from_dict(self.get_node(node_id))

    # ---- graph store surface used by the organize package ----
    def add_node(self, id, memory, metadata, user_name=None):
        props = dict(metadata)
        props.pop("embedding", None)
        for key, value in list(props.items()):
            if key == "history":
                props[key] = [
                    v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for v in value
                ]
            elif isinstance(value, dict):
                props[key] = json.dumps(value, ensure_ascii=False)
            elif key == "sources":
                props[key] = [
                    v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for v in value
                ]
        self.nodes.setdefault(id, {"memory": memory, "metadata": {}})
        self.nodes[id]["memory"] = memory
        self.nodes[id]["metadata"].update(props)
        self.payloads.setdefault(id, {}).update({"memory": memory, **props})

    def update_node(self, id, fields, user_name=None):
        for key, value in fields.items():
            _check_neo4j_value(key, value)
        node = self.nodes[id]
        fields = dict(fields)
        embedding = fields.pop("embedding", None)
        if "memory" in fields:
            node["memory"] = fields["memory"]
        node["metadata"].update({k: v for k, v in fields.items() if k != "memory"})
        self.payloads.setdefault(id, {}).update(fields)
        if embedding is not None:
            self.payloads[id]["vector_sync"] = "success"

    def get_node(self, id, include_embedding=False, **kwargs):
        node = self.nodes.get(id)
        if node is None:
            return None
        return {
            "id": id,
            "memory": node["memory"],
            "metadata": json.loads(json.dumps(node["metadata"])),
        }

    def get_nodes(self, ids, **kwargs):
        return [self.get_node(i) for i in ids if i in self.nodes]

    def delete_node(self, id, user_name=None):
        self.deleted.append(id)
        self.nodes.pop(id, None)
        self.payloads.pop(id, None)

    def search_by_embedding(
        self, vector, top_k=5, scope=None, status=None, threshold=None, user_name=None, **kw
    ):
        out = []
        for cid in self.neighbors.get(tuple(vector or ()), []):
            node = self.nodes.get(cid)
            if node is None:
                continue
            meta = node["metadata"]
            if status and meta.get("status", "activated") != status:
                continue
            if scope and meta.get("memory_type") != scope:
                continue
            out.append({"id": cid, "score": 0.9})
        return out[:top_k]

    def get_by_metadata(self, filters, user_name=None, status=None, **kwargs):
        assert not filters, "FakeGraph only models the status filter"
        return [
            node_id
            for node_id, node in self.nodes.items()
            if status is None or node["metadata"].get("status", "activated") == status
        ]

    def get_edges(self, id, type="ANY", direction="ANY", user_name=None):
        out = []
        for src, dst, t in self.edges:
            if type != "ANY" and t != type:
                continue
            if direction == "OUTGOING" and src != id:
                continue
            if direction == "INCOMING" and dst != id:
                continue
            if direction == "ANY" and id not in (src, dst):
                continue
            out.append({"from": src, "to": dst, "type": t})
        return out

    def edge_exists(self, source_id, target_id, type="ANY", direction="OUTGOING", user_name=None):
        for src, dst, t in self.edges:
            if type != "ANY" and t != type:
                continue
            if (src, dst) == (source_id, target_id):
                return True
            if direction == "ANY" and (dst, src) == (source_id, target_id):
                return True
        return False

    def add_edge(self, source_id, target_id, type, user_name=None):
        if (source_id, target_id, type) not in self.edges:
            self.edges.append((source_id, target_id, type))

    # ---- assertions helpers ----
    def status(self, node_id: str) -> str:
        return self.nodes[node_id]["metadata"].get("status", "activated")

    def history(self, node_id: str) -> list[dict[str, Any]]:
        return [json.loads(h) for h in self.nodes[node_id]["metadata"].get("history", [])]

    def merged_to(self) -> list[tuple[str, str]]:
        return [(s, d) for s, d, t in self.edges if t == "MERGED_TO"]
