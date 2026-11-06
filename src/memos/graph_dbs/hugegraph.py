# Recorded divergences from the neo4j_community baseline (see commit for the
# full parity evidence; each divergence is also noted at its method):
#   - search_by_fulltext is stronger: the baseline returns [] (its own TODO).
#   - delete_node_by_prams raises ValueError for non-str writable_cube_ids;
#     the baseline silently deletes 0 rows.
#   - get_neighbors_by_tag raises TypeError on exclude_ids=None; the
#     baseline's WHERE NOT n.id IN null matches nothing.
#   - deduplicate_nodes / detect_conflicts / merge_nodes stay
#     NotImplementedError like the baseline; get_k_hop_neighbors only
#     populates `neighbors` (no baseline, no caller).

import heapq
import itertools
import json
import re
import time

from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Literal

from memos.configs.graph_db import HugeGraphDBConfig
from memos.dependency import require_python_package
from memos.graph_dbs.base import BaseGraphDB
from memos.log import get_logger
from memos.utils import normalize_to_utc_iso
from memos.vec_dbs.index import BaseVectorIndex, VectorIndexFactory
from memos.vec_dbs.item import VecDBItem


logger = get_logger(__name__)

_VERTEX_LABEL = "Memory"
_EDGE_LABEL = "MEMORY_RELATION"
_RELATION_TYPE_KEY = "relation_type"
_VALID_PROPERTY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# The schema this backend queries by. Mirrors docker/hugegraph-schema.groovy,
# which until now was the only definition and had to be loaded by hand before
# a fresh graph could serve a single write.
_SCHEMA_PROPERTY_KEYS = (
    "id",
    "memory",
    "user_name",
    "memory_type",
    "status",
    "created_at",
    "updated_at",
    "metadata_json",
    "embedding_json",
    "mem_cube_id",
    "relation_type",
    "delete_record_id",
    "delete_time",
)
_SCHEMA_MEMORY_NULLABLE_KEYS = (
    "memory",
    "user_name",
    "memory_type",
    "status",
    "created_at",
    "updated_at",
    "metadata_json",
    "embedding_json",
    "mem_cube_id",
    "delete_record_id",
    "delete_time",
)
_SCHEMA_MEMORY_PROPERTIES = ("id", *_SCHEMA_MEMORY_NULLABLE_KEYS)

# get_by_metadata pushdown bindings (live-probed); the engine has no
# prefix/suffix/regex predicate, and range predicates only compare text.
_METADATA_PUSHDOWN_OPS = {"=", "in", ">", ">=", "<", "<=", "contains"}
_GREMLIN_RANGE_PREDICATES = {">": "P.gt", ">=": "P.gte", "<": "P.lt", "<=": "P.lte"}
# "id" is reserved in gremlin has(); metadata_json is one JSON blob, not a
# per-field property — both stay client-side.
_METADATA_CLIENT_ONLY_FIELDS = {"id", "metadata_json"}
# (name, on vertex or edge label, indexed fields, index type). No user_name
# -alone index: the server rejects indexes covered by a composite.
_SCHEMA_INDEXES = (
    ("memoryByMemoryType", _VERTEX_LABEL, ("memory_type",), "secondary"),
    ("memoryByStatus", _VERTEX_LABEL, ("status",), "secondary"),
    ("memoryByUserAndType", _VERTEX_LABEL, ("user_name", "memory_type"), "secondary"),
    ("memoryByUserAndStatus", _VERTEX_LABEL, ("user_name", "status"), "secondary"),
    ("memoryByMemCubeId", _VERTEX_LABEL, ("mem_cube_id",), "secondary"),
    ("relationByUser", _EDGE_LABEL, ("user_name",), "secondary"),
    ("memoryByText", _VERTEX_LABEL, ("memory",), "search"),
)

# Replicas disagree about freshly created indexes for a long time; a lagging
# one rejects the query at planning time, so retrying the same query is safe.
_INDEX_NOT_READY_RE = re.compile(r"in status [A-Z_]+\(CREATED expected\)")
_INDEX_RETRY_ATTEMPTS = 5
_INDEX_RETRY_SECONDS = 1.5


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _pack_metadata(metadata: dict[str, Any]) -> str:
    reserved = {
        "memory_type",
        "status",
        "user_name",
        "embedding",
        "created_at",
        "updated_at",
        "mem_cube_id",
        "delete_record_id",
        "delete_time",
    }
    return json.dumps(
        {k: v for k, v in metadata.items() if k not in reserved}, ensure_ascii=False, default=str
    )


def _unpack_node(props: dict[str, Any], include_embedding: bool = False) -> dict[str, Any]:
    node_id = props.get("id", "")
    memory = props.get("memory", "")
    metadata: dict[str, Any] = {}
    with suppress(TypeError, json.JSONDecodeError):
        metadata.update(json.loads(props.get("metadata_json") or "{}"))
    for key in (
        "memory_type",
        "status",
        "user_name",
        "created_at",
        "updated_at",
        "mem_cube_id",
        "delete_record_id",
        "delete_time",
    ):
        if props.get(key):
            metadata[key] = props[key]
    if include_embedding and props.get("embedding_json"):
        with suppress(TypeError, json.JSONDecodeError):
            metadata["embedding"] = json.loads(props["embedding_json"])
    return {"id": node_id, "memory": memory, "metadata": metadata}


class HugeGraphGraphDB(BaseGraphDB):
    """HugeGraph-backed graph memory store using pyhugegraph (hugegraph-python)."""

    # Closed: schema/index bootstrap now runs from __init__ (_ensure_schema
    # below), the counterpart of the Neo4j backend's create_index /
    # _create_basic_property_indexes. docker/hugegraph-schema.groovy remains as
    # the human-readable DDL for Gremlin Console users. Still true from that
    # note: the HugeGraph 1.7 custom-UUID workaround in
    # _get_vertex_by_node_id below, which would go away with an id index.

    @require_python_package(
        import_name="pyhugegraph",
        install_command="pip install 'MemoryOS[hugegraph]'",
        install_link="https://github.com/apache/hugegraph-ai",
    )
    def __init__(
        self,
        config: HugeGraphDBConfig,
        vector_index: BaseVectorIndex | None = None,
    ):
        from pyhugegraph.client import PyHugeClient

        self.config = config
        self.user_name = config.user_name
        # hugegraph-python 1.7.0 replaced 1.5.0's ip/port pair with a single
        # url; pyproject pins >=1.7.0 for exactly that reason, so the config
        # fields stay as ip/port and get joined here.
        self._client = PyHugeClient(
            url=f"http://{config.ip}:{config.port}",
            graph=config.graph,
            user=config.user or "",
            pwd=config.password or "",
            graphspace=config.graphspace or None,
        )
        self._g = self._client.graph()
        self._gremlin = self._client.gremlin()
        self._traverser = self._client.traverser()

        # The graph provider depends on a backend-neutral vector index. The
        # factory is only a convenience for config-driven construction; callers
        # can inject any BaseVectorIndex implementation directly.
        self._vector_index = vector_index
        if self._vector_index is None and config.vec_config:
            self._vector_index = VectorIndexFactory.from_config(config.vec_config)
        # The Neo4j backends expose their raw VecDB as ``vec_db``, and
        # organize/handler.py reaches for that name via getattr to sync a merged
        # loser's payload status. Without the same attribute here the getattr
        # default turns that sync into a silent no-op, which is precisely the
        # ghost-activated bug the sync exists to prevent.
        self.vec_db = self._vector_index.vec_db if self._vector_index else None
        if self._vector_index is None:
            # The Neo4j configs require vec_config outright, so running this
            # backend vector-less is a deliberate, unusual choice. Say so at
            # startup, and let the vector paths below fail loudly instead of
            # quietly dropping embeddings or returning empty recall.
            logger.warning(
                "[hugegraph] no vector index configured: search_by_embedding "
                "and embedding writes will raise until one is configured"
            )

        self._ensure_schema()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create the property keys, labels and indexes this backend queries by.

        Counterpart to the Neo4j backend's ``create_index`` /
        ``_create_basic_property_indexes``, which run from ``__init__`` and rely
        on ``IF NOT EXISTS`` for idempotency. Like the Neo4j version this runs on
        every construction; the statements are cheap and no in-repo path
        constructs a store per request. Unlike the Neo4j version, failures
        raise: those statements only add indexes, while a missing property key
        here makes every subsequent write fail with "Undefined property key",
        so warning-and-continuing would trade a clear startup error for a
        confusing one on first write.

        Existence is decided from the four list endpoints rather than one GET
        per object. The list is what the server's own create-time duplicate
        check sees, while a single-object GET can come back from a replica
        whose schema cache lags — the second construction then POSTs something
        that exists and dies on "Repeated new index label". Lists also turn 19
        existence probes into 4 calls. As with ``IF NOT EXISTS``, this ensures
        existence only: an object that exists with drifted properties is left
        alone, and a fresh graph is expected.
        """
        schema = self._client.schema()
        existing_keys = {item.name for item in (schema.getPropertyKeys() or [])}
        existing_vertex_labels = {item.name for item in (schema.getVertexLabels() or [])}
        existing_edge_labels = {item.name for item in (schema.getEdgeLabels() or [])}
        existing_indexes = {item.name for item in (schema.getIndexLabels() or [])}

        for name in _SCHEMA_PROPERTY_KEYS:
            if name not in existing_keys:
                schema.propertyKey(name).asText().ifNotExist().create()
        if _VERTEX_LABEL not in existing_vertex_labels:
            schema.vertexLabel(_VERTEX_LABEL).useCustomizeStringId().properties(
                *_SCHEMA_MEMORY_PROPERTIES
            ).nullableKeys(*_SCHEMA_MEMORY_NULLABLE_KEYS).ifNotExist().create()
        if _EDGE_LABEL not in existing_edge_labels:
            schema.edgeLabel(_EDGE_LABEL).sourceLabel(_VERTEX_LABEL).targetLabel(
                _VERTEX_LABEL
            ).properties(_RELATION_TYPE_KEY, "user_name").nullableKeys(
                "user_name"
            ).ifNotExist().create()
        for name, base, fields, index_type in _SCHEMA_INDEXES:
            if name in existing_indexes:
                continue
            builder = schema.indexLabel(name)
            builder = builder.onV(base) if base == _VERTEX_LABEL else builder.onE(base)
            builder = builder.by(*fields)
            builder = builder.secondary() if index_type == "secondary" else builder.search()
            try:
                builder.ifNotExist().create()
            except Exception as exc:
                # A lagging replica can report the index as missing in the list
                # while another replica has already created it, and the server
                # then rejects the re-create with "Repeated new index label
                # <name>(...) due to existed index label ...". That is the
                # already-exists outcome, not a bootstrap failure.
                if "Repeated new index label" not in str(exc):
                    raise
                logger.info("[schema] index %s already exists per the server", name)
        # The Neo4j version keeps the vector payload indexes in step here too.
        if self.vec_db is not None and hasattr(self.vec_db, "ensure_payload_indexes"):
            self.vec_db.ensure_payload_indexes(["user_name", "memory_type", "status"])

    def check_health(self) -> None:
        """Raise when graph or configured vector storage cannot serve requests."""
        self._gremlin_exec("g.V().limit(1).count()")
        if self._vector_index is None:
            return
        self._vector_index.check_health()

    def _tenant(self, user_name: str | None = None) -> str:
        return user_name or self.user_name

    @staticmethod
    def _vector_payload(metadata: dict[str, Any], tenant: str) -> dict[str, Any]:
        """Return the fields used to isolate and filter semantic recall.

        ``vector_sync`` mirrors the Neo4j backend, whose recall path filters on
        it to skip vectors that never synced; here a payload only exists when
        the write succeeded, so the value is constant.
        """
        return {
            "user_name": tenant,
            "memory_type": metadata.get("memory_type", ""),
            "status": metadata.get("status", ""),
            "mem_cube_id": metadata.get("mem_cube_id", ""),
            "vector_sync": "success",
        }

    # ------------------------------------------------------------------
    # Vector index coordination
    # ------------------------------------------------------------------

    def _vec_add(self, items: list) -> None:
        """Upsert vectors through the injected backend-neutral index."""
        if not items:
            return
        if self._vector_index is None:
            # A caller handing over embeddings with nowhere to put them would
            # otherwise write graph rows that vector recall can never see.
            raise RuntimeError(
                "HugeGraph backend has no vector index configured, "
                "so embedding writes cannot be stored; set vec_config or "
                "pass vector_index"
            )
        self._vector_index.upsert(items)

    def _vec_delete(self, ids: list[str]) -> None:
        """Delete by IDs via whichever backend is configured."""
        # Deliberately a no-op without an index, unlike _vec_add: a store that
        # never accepted embedding writes has no vectors to delete, and
        # raising here would break delete_node/clear for graph-only data.
        if self._vector_index is None or not ids:
            return
        self._vector_index.delete(ids)

    def _vec_search(self, vector: list[float], top_k: int, payload_filter: dict) -> list:
        """Search through the injected backend-neutral vector index."""
        if self._vector_index is None:
            # Silent emptiness here is how semantic recall used to fail: the
            # caller cannot tell "no matches" from "no index".
            raise RuntimeError(
                "HugeGraph backend has no vector index configured, "
                "so semantic recall cannot run; set vec_config or pass "
                "vector_index"
            )
        return self._vector_index.search(vector, top_k, payload_filter)

    @staticmethod
    def _gremlin_literal(value: Any) -> str:
        """Encode data as a Groovy/Gremlin literal without executable interpolation."""
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _property_name(value: str) -> str:
        if not _VALID_PROPERTY_NAME.fullmatch(value):
            raise ValueError(f"Invalid HugeGraph property name: {value!r}")
        return value

    _GREMLIN_PARAM_LIMIT = 250  # live-probed: 253 ids OK, 254 → "bad parameter count 256"
    # Indexed within() conditions face a second, tighter cap: has('user_name',
    # within(N)) passes at N=100 and dies at 101 with "Too many conditions(101)
    # each query" (live-probed on 1.8.0 HStore).
    _GREMLIN_INDEX_CONDITION_LIMIT = 100

    @classmethod
    def _chunked(cls, ids: list[str], limit: int | None = None) -> "list[list[str]]":
        """Split an id list under the engine's Groovy parameter-count cap.

        Live-probed on 1.8.0 HStore: hasId(within(...)) with 253 ids passes,
        254 gets rejected with "bad parameter count 256" — so every
        within(*ids) traversal must chunk, imports and deletes alike.
        Pass ``limit`` when the list feeds indexed within() conditions instead,
        which hit the tighter index-condition cap before the parameter cap.
        """
        cap = cls._GREMLIN_PARAM_LIMIT if limit is None else limit
        return [ids[start : start + cap] for start in range(0, len(ids), cap)]

    @classmethod
    def _split_within_clause(cls, name: str, values: list) -> str:
        """Build has(name, within(values)), or()ing chunks beyond the cap.

        The indexed within() cap (live-probed: 101 values → "Too many
        conditions(101) each query") applies per clause, and two within(100)
        clauses coexist in one query — so an oversized list becomes an or()
        of disjoint within() chunks. Disjoint chunks cannot match the same
        vertex twice, so or() cannot double-emit (the engine dedups within()
        or() branches, unlike Text.contains or()).

        Live-probed ceiling: ~2203 values (23 or() branches) pass; ~2303
        values (24 branches) hit an empty-message ServerError — split across
        queries before that.
        """
        chunks = cls._chunked(list(values), cls._GREMLIN_INDEX_CONDITION_LIMIT)
        if len(chunks) == 1:
            joined = ", ".join(cls._gremlin_literal(v) for v in chunks[0])
            return f".has('{cls._property_name(name)}', within({joined}))"
        return (
            ".or("
            + ", ".join(
                f"__.has('{cls._property_name(name)}', "
                f"within({', '.join(cls._gremlin_literal(v) for v in chunk)}))"
                for chunk in chunks
            )
            + ")"
        )

    def _gremlin_exec(self, query: str) -> list:
        result = self._exec_with_index_ready_retry(query)
        # pyhugegraph hands back the gremlin response envelope — the server's
        # "result" object, {"data": [...rows...], "meta": ...} — while every
        # caller here wants the row list inside it. Wrapping instead of
        # unwrapping meant callers saw the envelope as their one and only row:
        # _get_vertex_by_node_id read {"data": [], "meta": {}} as a found
        # vertex, so add_node appended to a vertex that did not exist.
        if result is None:
            return []
        if isinstance(result, dict):
            if "data" not in result:
                raise TypeError(f"Unexpected gremlin result shape: {result!r}")
            data = result["data"]
            if not isinstance(data, list):
                raise TypeError(f"Unexpected gremlin result shape: {result!r}")
            return data
        if isinstance(result, list):
            return result
        raise TypeError(f"Unexpected gremlin result shape: {result!r}")

    def _exec_with_index_ready_retry(self, query: str) -> Any:
        """Run a gremlin query, retrying while a replica reports an index not CREATED.

        See the comment on ``_INDEX_NOT_READY_RE`` for the measured behaviour;
        without the retry the first queries after a bootstrap fail roughly a
        quarter of the time until every replica's schema cache catches up.
        """
        for attempt in range(_INDEX_RETRY_ATTEMPTS):
            try:
                return self._gremlin.exec(query)
            except Exception as exc:
                if not _INDEX_NOT_READY_RE.search(str(exc)):
                    raise
                if attempt + 1 >= _INDEX_RETRY_ATTEMPTS:
                    raise
                logger.warning(
                    "[gremlin] replica reports an index as not CREATED yet, retrying (%d/%d)",
                    attempt + 1,
                    _INDEX_RETRY_ATTEMPTS - 1,
                )
                time.sleep(_INDEX_RETRY_SECONDS)
        raise RuntimeError("unreachable: the retry loop always returns or raises")

    def _delete_matching_vertices(self, traversal: str) -> int:
        """Keep vector cleanup ahead of the matching graph deletion."""
        ids = [str(node_id) for node_id in self._gremlin_exec(traversal + ".values('id')")]
        self._vec_delete(ids)
        self._gremlin_exec(traversal + ".drop()")
        return len(ids)

    def _delete_selected_vertices(self, rows: list[dict[str, Any]]) -> int:
        """Delete valueMap rows already filtered client-side, chunked."""
        ids = [str(self._parse_valuemap(row).get("id", "")) for row in rows]
        ids = [i for i in ids if i]
        if not ids:
            return 0
        self._vec_delete(ids)
        for chunk in self._chunked(ids):
            id_groups = ", ".join(self._gremlin_literal(i) for i in chunk)
            self._gremlin_exec(f"g.V().hasId(within(*[{id_groups}])).drop()")
        return len(ids)

    def _sync_vector_status(self, rows: list[dict[str, Any]], tenant: str, status: str) -> None:
        if self._vector_index is None:
            return

        items = []
        for row in rows:
            props = self._parse_valuemap(row)
            try:
                embedding = json.loads(props.get("embedding_json") or "")
            except (TypeError, json.JSONDecodeError):
                continue
            vector_meta = {
                "memory_type": props.get("memory_type", ""),
                "status": status,
                "mem_cube_id": props.get("mem_cube_id", ""),
            }
            items.append(
                VecDBItem(
                    id=str(props.get("id", "")),
                    vector=embedding,
                    payload=self._vector_payload(vector_meta, tenant),
                )
            )
        if items:
            self._vec_add(items)

    def _get_vertex_by_node_id(self, node_id: str, user_name: str | None = None) -> dict | None:
        tenant = self._tenant(user_name)
        # REST answers authoritatively here: the gremlin point lookup has
        # been wrong in both directions live (1.7 could not resolve custom
        # UUID ids; 1.8 keeps deleted vertices visible to g.V for 45s+), so
        # an empty REST list means absent and gremlin only covers REST
        # call failures.
        try:
            vertices = self._g.getVerticesById([node_id]) or []
        except Exception:  # any REST failure (auth/conn/HTTP) routes to gremlin
            logger.warning(
                "[hugegraph] REST vertex read failed for %s, falling back to gremlin",
                node_id,
                exc_info=True,
            )
            q = (
                f"g.V({self._gremlin_literal(node_id)})"
                f".has('user_name', {self._gremlin_literal(tenant)}).valueMap(true).limit(1)"
            )
            rows = self._gremlin_exec(q)
            if not rows:
                return None
            return self._parse_valuemap(rows[0])
        for vertex in vertices:
            props = getattr(vertex, "properties", None)
            if isinstance(props, dict) and props.get("user_name") == tenant:
                return props
        return None

    @staticmethod
    def _parse_valuemap(row: dict) -> dict[str, Any]:
        """Flatten gremlin valueMap(true) result into a plain dict."""
        result: dict[str, Any] = {}
        for k, v in row.items():
            if k in ("id", "label"):
                result[k] = v[0] if isinstance(v, list) and len(v) == 1 else v
            elif isinstance(v, list):
                result[k] = v[0] if len(v) == 1 else v
            else:
                result[k] = v
        return result

    # ------------------------------------------------------------------
    # Node Management
    # ------------------------------------------------------------------

    def add_node(
        self, id: str, memory: str, metadata: dict[str, Any], user_name: str | None = None
    ) -> None:
        tenant = self._tenant(user_name)
        meta = metadata.copy()
        embedding = meta.pop("embedding", None)

        props = {
            "id": id,
            "memory": memory,
            "user_name": tenant,
            "memory_type": meta.get("memory_type", ""),
            "status": meta.get("status", ""),
            "mem_cube_id": meta.get("mem_cube_id", ""),
            "delete_record_id": meta.get("delete_record_id", ""),
            "delete_time": meta.get("delete_time", ""),
            "created_at": meta.get("created_at", _now_iso()),
            "updated_at": meta.get("updated_at", _now_iso()),
            "metadata_json": _pack_metadata(meta),
            "embedding_json": json.dumps([float(x) for x in embedding], ensure_ascii=False)
            if embedding
            else "",
        }

        if self._get_vertex_by_node_id(id, user_name) is None:
            self._g.addVertex(_VERTEX_LABEL, props, id=id)
        else:
            # Retrying after a vector failure repairs the vector record and
            # refreshes the already-created graph vertex idempotently.
            self._g.appendVertex(id, props)

        if embedding:
            self._vec_add(
                [VecDBItem(id=id, vector=embedding, payload=self._vector_payload(meta, tenant))]
            )

    def add_nodes_batch(
        self, nodes: list[dict[str, Any]], user_name: str | None = None
    ) -> list[str]:
        if not nodes:
            return []
        tenant = self._tenant(user_name)
        batch = []
        vec_items = []

        for node in nodes:
            nid = node["id"]
            memory = node["memory"]
            meta = node.get("metadata", {}).copy()
            embedding = meta.pop("embedding", None)
            props = {
                "id": nid,
                "memory": memory,
                "user_name": tenant,
                "memory_type": meta.get("memory_type", ""),
                "status": meta.get("status", ""),
                "mem_cube_id": meta.get("mem_cube_id", ""),
                "delete_record_id": meta.get("delete_record_id", ""),
                "delete_time": meta.get("delete_time", ""),
                "created_at": meta.get("created_at", _now_iso()),
                "updated_at": meta.get("updated_at", _now_iso()),
                "metadata_json": _pack_metadata(meta),
                "embedding_json": json.dumps([float(x) for x in embedding], ensure_ascii=False)
                if embedding
                else "",
            }
            batch.append((nid, props))
            if embedding:
                vec_items.append(
                    VecDBItem(id=nid, vector=embedding, payload=self._vector_payload(meta, tenant))
                )

        # PyHugeClient.addVertices() does not accept custom vertex IDs. Memory
        # vertices use CUSTOMIZE_STRING IDs, so preserve each MemOS UUID through
        # the public addVertex() API instead of reaching into the private session.
        node_ids = [node_id for node_id, _ in batch]
        # Existence comes from the REST batch read: the gremlin probe
        # resurrects recently deleted ids (replica lag) and would misroute
        # re-imports. Large batches are chunked because the REST read rides
        # the query string (~8KB request-line limit); if REST fails, fall
        # back to the gremlin probe — a wrong "exists" verdict degrades to
        # an appendVertex error on that single vertex.
        existing_ids: set[str] = set()

        def _rest_existing(ids: list[str]) -> set[str]:
            found: set[str] = set()
            for vertex in self._g.getVerticesById(ids) or []:
                props = getattr(vertex, "properties", None)
                if isinstance(props, dict) and props.get("user_name") == tenant:
                    found.add(str(props.get("id")))
            return found

        chunk_size = 100
        try:
            for start in range(0, len(node_ids), chunk_size):
                existing_ids |= _rest_existing(node_ids[start : start + chunk_size])
        except Exception:
            logger.warning(
                "[hugegraph] REST existence probe failed for %d ids, falling back to gremlin",
                len(node_ids),
                exc_info=True,
            )
            for chunk in self._chunked(node_ids):
                id_groups = ", ".join(self._gremlin_literal(i) for i in chunk)
                gremlin_q = (
                    f"g.V().hasId(within(*[{id_groups}]))"
                    f".has('user_name', {self._gremlin_literal(tenant)}).values('id')"
                )
                existing_ids |= {str(node_id) for node_id in self._gremlin_exec(gremlin_q)}
        for node_id, props in batch:
            if node_id in existing_ids:
                self._g.appendVertex(node_id, props)
            else:
                self._g.addVertex(_VERTEX_LABEL, props, id=node_id)
        if vec_items:
            self._vec_add(vec_items)
        return node_ids

    def update_node(self, id: str, fields: dict[str, Any], user_name: str | None = None) -> None:
        tenant = self._tenant(user_name)
        existing = self._get_vertex_by_node_id(id, user_name)
        if existing is None:
            return
        fields = fields.copy()
        embedding = fields.pop("embedding", None)

        props = {}
        direct_keys = {
            "memory_type",
            "status",
            "mem_cube_id",
            "delete_record_id",
            "delete_time",
            "created_at",
            "updated_at",
            "memory",
        }
        vector_filter_changed = bool({"memory_type", "status", "mem_cube_id"} & fields.keys())
        for k in direct_keys:
            if k in fields:
                props[k] = fields.pop(k)
        props["updated_at"] = _now_iso()

        # Re-fetch existing metadata_json to merge
        try:
            current_meta = json.loads(existing.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            current_meta = {}
        current_meta.update(fields)
        props["metadata_json"] = json.dumps(current_meta, ensure_ascii=False, default=str)

        if embedding:
            props["embedding_json"] = json.dumps([float(x) for x in embedding], ensure_ascii=False)

        self._g.appendVertex(id, props)

        vector = embedding
        if vector is None and vector_filter_changed and existing.get("embedding_json"):
            try:
                vector = json.loads(existing["embedding_json"])
            except (TypeError, json.JSONDecodeError):
                vector = None

        if vector:
            vector_meta = {
                "memory_type": props.get("memory_type", existing.get("memory_type", "")),
                "status": props.get("status", existing.get("status", "")),
                "mem_cube_id": props.get("mem_cube_id", existing.get("mem_cube_id", "")),
            }
            self._vec_add(
                [VecDBItem(id=id, vector=vector, payload=self._vector_payload(vector_meta, tenant))]
            )

    def delete_node(self, id: str, user_name: str | None = None) -> None:
        if self._get_vertex_by_node_id(id, user_name) is None:
            return
        self._vec_delete([id])
        self._g.removeVertexById(id)

    # ------------------------------------------------------------------
    # Edge Management
    # ------------------------------------------------------------------

    def add_edge(
        self, source_id: str, target_id: str, type: str, user_name: str | None = None
    ) -> None:
        tenant = self._tenant(user_name)
        if (
            self._get_vertex_by_node_id(source_id, user_name) is None
            or self._get_vertex_by_node_id(target_id, user_name) is None
        ):
            logger.warning(
                "Skip cross-tenant or missing edge endpoints: %s -> %s", source_id, target_id
            )
            return
        self._g.addEdge(
            _EDGE_LABEL, source_id, target_id, {_RELATION_TYPE_KEY: type, "user_name": tenant}
        )

    def delete_edge(
        self, source_id: str, target_id: str, type: str, user_name: str | None = None
    ) -> None:
        tenant = self._tenant(user_name)
        source = self._gremlin_literal(source_id)
        target = self._gremlin_literal(target_id)
        relation = self._gremlin_literal(type)
        tenant_literal = self._gremlin_literal(tenant)
        q = (
            f"g.V({source}).outE('{_EDGE_LABEL}')"
            f".has('{_RELATION_TYPE_KEY}', {relation})"
            f".where(inV().hasId({target}))"
            f".has('user_name', {tenant_literal}).drop()"
        )
        self._gremlin_exec(q)

    def edge_exists(
        self,
        source_id: str,
        target_id: str,
        type: str,
        direction: str = "OUTGOING",
        user_name: str | None = None,
    ) -> bool:
        tenant = self._tenant(user_name)
        source = self._gremlin_literal(source_id)
        target = self._gremlin_literal(target_id)
        relation = self._gremlin_literal(type)
        tenant_literal = self._gremlin_literal(tenant)
        if direction == "OUTGOING":
            q = (
                f"g.V({source}).outE('{_EDGE_LABEL}')"
                f".has('{_RELATION_TYPE_KEY}', {relation})"
                f".has('user_name', {tenant_literal})"
                f".where(inV().hasId({target})).limit(1).count()"
            )
        elif direction == "INCOMING":
            q = (
                f"g.V({source}).inE('{_EDGE_LABEL}')"
                f".has('{_RELATION_TYPE_KEY}', {relation})"
                f".has('user_name', {tenant_literal})"
                f".where(outV().hasId({target})).limit(1).count()"
            )
        else:
            q = (
                f"g.V({source}).bothE('{_EDGE_LABEL}')"
                f".has('{_RELATION_TYPE_KEY}', {relation})"
                f".has('user_name', {tenant_literal})"
                f".where(otherV().hasId({target})).limit(1).count()"
            )
        result = self._gremlin_exec(q)
        return bool(result and result[0])

    # ------------------------------------------------------------------
    # Node Queries
    # ------------------------------------------------------------------

    def get_node(self, id: str, include_embedding: bool = False, **kwargs) -> dict[str, Any] | None:
        user_name = kwargs.get("user_name")
        props = self._get_vertex_by_node_id(id, user_name)
        if props is None:
            return None
        return _unpack_node(props, include_embedding)

    def get_nodes(
        self, ids: list[str], include_embedding: bool = False, **kwargs
    ) -> list[dict[str, Any]]:
        if not ids:
            return []
        vertices = self._g.getVerticesById(ids)
        if not vertices:
            return []
        result = []
        for v in vertices:
            if (
                v
                and v.properties
                and v.properties.get("user_name") == self._tenant(kwargs.get("user_name"))
            ):
                result.append(_unpack_node(v.properties, include_embedding))
        return result

    def get_edges(
        self, id: str, type: str = "ANY", direction: str = "ANY", user_name: str | None = None
    ) -> list[dict[str, str]]:
        tenant = self._tenant(user_name)
        node_literal = self._gremlin_literal(id)
        tenant_literal = self._gremlin_literal(tenant)
        edge_filter = f"('{_EDGE_LABEL}')"
        type_filter = (
            "" if type == "ANY" else f".has('{_RELATION_TYPE_KEY}', {self._gremlin_literal(type)})"
        )
        user_filter = f".has('user_name', {tenant_literal})"

        if direction == "OUTGOING":
            q = f"g.V({node_literal}).outE{edge_filter}{type_filter}{user_filter}.project('from','to','type').by(outV().id()).by(inV().id()).by('{_RELATION_TYPE_KEY}')"
        elif direction == "INCOMING":
            q = f"g.V({node_literal}).inE{edge_filter}{type_filter}{user_filter}.project('from','to','type').by(outV().id()).by(inV().id()).by('{_RELATION_TYPE_KEY}')"
        else:
            q = (
                f"g.V({node_literal}).bothE{edge_filter}{type_filter}{user_filter}"
                f".project('from','to','type')"
                f".by(outV().id()).by(inV().id()).by('{_RELATION_TYPE_KEY}')"
            )

        rows = self._gremlin_exec(q)
        edges = []
        for row in rows:
            if isinstance(row, dict) and "from" in row:
                edges.append({"from": row["from"], "to": row["to"], "type": row.get("type", "")})
        return edges

    def search_by_kneighbor(
        self,
        source_id: str,
        *,
        max_depth: int = 1,
        limit: int = 10,
        user_name: str | None = None,
        status: str | None = None,
        max_degree: int = 10000,
        skip_degree: int = 0,
    ) -> dict[str, Any]:
        """Run HugeGraph's built-in K-neighbor traverser through PyHugeClient.

        MemOS keeps the stable response shape expected by the recall adapter
        and filters the returned IDs through the public Gremlin manager.
        """
        # TODO(hugegraph): only `neighbors` is populated; `paths`, `vertices`
        # and `edges` are always returned empty, and the traverser response is
        # not paginated by `limit` on the server side. Nothing in MemOS calls
        # this method today, so the shape was never validated against a real
        # consumer.
        depth = max(1, int(max_depth))
        result_limit = max(1, int(limit))
        degree = max(1, int(max_degree))
        skipped_degree = max(0, int(skip_degree))
        if skipped_degree and skipped_degree < degree:
            raise ValueError("skip_degree must be 0 or greater than or equal to max_degree")

        response = self._traverser.k_neighbor(str(source_id), depth)
        if not isinstance(response, dict):
            return {
                "source": str(source_id),
                "neighbors": [],
                "paths": [],
                "vertices": [],
                "edges": [],
            }
        payload = response.get("data", response)
        if not isinstance(payload, dict):
            payload = {}
        neighbors = [
            str(item.get("id")) if isinstance(item, dict) and item.get("id") else str(item)
            for item in payload.get("kneighbor", []) or []
            if str(item.get("id") if isinstance(item, dict) else item) != str(source_id)
        ]
        if neighbors:
            tenant = self._tenant(user_name)
            allowed_ids: set[str] = set()
            for chunk in self._chunked(neighbors):
                id_groups = ", ".join(self._gremlin_literal(i) for i in chunk)
                filter_query = (
                    f"g.V().hasId(within(*[{id_groups}]))"
                    f".hasLabel('{_VERTEX_LABEL}')"
                    f".has('user_name', {self._gremlin_literal(tenant)})"
                )
                if status:
                    filter_query += f".has('status', {self._gremlin_literal(status)})"
                allowed_ids |= {
                    str(node_id) for node_id in self._gremlin_exec(filter_query + ".id()")
                }
            neighbors = [node_id for node_id in neighbors if node_id in allowed_ids]
        neighbors = neighbors[:result_limit]
        return {
            "source": str(source_id),
            "neighbors": neighbors,
            "paths": [],
            "vertices": [],
            "edges": [],
        }

    def get_neighbors(
        self,
        id: str,
        type: str,
        direction: Literal["in", "out", "both"] = "out",
        user_name: str | None = None,
    ) -> list[str]:
        tenant = self._tenant(user_name)
        step = {"out": "out", "in": "in", "both": "both"}.get(direction, "out")
        q = (
            f"g.V({self._gremlin_literal(id)}).{step}E('{_EDGE_LABEL}')"
            f".has('{_RELATION_TYPE_KEY}', {self._gremlin_literal(type)})"
            f".has('user_name', {self._gremlin_literal(tenant)})"
            f".otherV().has('user_name', {self._gremlin_literal(tenant)}).values('id')"
        )
        return self._gremlin_exec(q)

    def get_path(
        self, source_id: str, target_id: str, max_depth: int = 3, user_name: str | None = None
    ) -> list[str]:
        tenant = self._tenant(user_name)
        depth = max(0, int(max_depth))
        target = self._gremlin_literal(target_id)
        tenant_literal = self._gremlin_literal(tenant)
        q = (
            f"g.V({self._gremlin_literal(source_id)})"
            f".has('user_name', {tenant_literal})"
            f".repeat(bothE('{_EDGE_LABEL}').has('user_name', {tenant_literal})"
            f".otherV().has('user_name', {tenant_literal}).simplePath())"
            f".until(hasId({target}).or().loops().is(gte({depth})))"
            f".hasId({target}).limit(1).path().by('id')"
        )
        rows = self._gremlin_exec(q)
        if rows and isinstance(rows[0], dict):
            return rows[0].get("objects", [])
        return []

    def get_subgraph(
        self,
        center_id: str,
        depth: int = 2,
        center_status: str = "activated",
        user_name: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve a local subgraph centered at a given node.

        ``center_status`` gates the center node only; neighbours come back
        regardless of their own status, matching ``Neo4jGraphDB.get_subgraph``.
        ``TreeTextMemory.search`` always passes it by keyword, so the parameter
        has to exist even to disable the filter — pass ``None`` for that.
        """
        tenant = self._tenant(user_name)
        depth = max(0, int(depth))
        tenant_literal = self._gremlin_literal(tenant)

        core_raw = self._get_vertex_by_node_id(center_id, user_name)
        core_node = _unpack_node(core_raw) if core_raw else None
        # The Neo4j backend folds the status test into the center MATCH, so an
        # ineligible center yields no rows at all: no core node and no
        # neighbours. Return that same shape instead of a subgraph hanging off
        # a center the caller is not allowed to use.
        if core_node is None or (
            center_status and core_node["metadata"].get("status") != center_status
        ):
            return {"core_node": None, "neighbors": [], "edges": []}

        # Live 1.8.0 HStore: edge elementMaps carry IN/OUT (not outV/inV), so
        # vertex and edge objects are told apart by the "label" they carry
        # (Memory vs MEMORY_RELATION) and edge endpoints come from IN/OUT.
        q = (
            f"g.V({self._gremlin_literal(center_id)})"
            f".has('user_name', {tenant_literal})"
            f".repeat(bothE('{_EDGE_LABEL}').has('user_name', {tenant_literal})"
            f".otherV().has('user_name', {tenant_literal}).simplePath())"
            f".emit().times({depth})"
            f".path().by(elementMap()).by(elementMap())"
        )
        rows = self._gremlin_exec(q)

        neighbors = []
        edges = []
        seen_nodes: set[str] = set()
        seen_edges: set[tuple] = set()

        for path in rows:
            objs = path.get("objects", []) if isinstance(path, dict) else []
            for obj in objs:
                if not isinstance(obj, dict):
                    continue
                if obj.get("label") == _EDGE_LABEL:
                    # Live 1.8.0 elementMap wraps the endpoints as
                    # {"id": ..., "label": ...} dicts (probed on memos_verify);
                    # accept bare ids and list-wrapped ids defensively.
                    source = obj.get("OUT")
                    target = obj.get("IN")
                    source_id = source.get("id") if isinstance(source, dict) else source
                    target_id = target.get("id") if isinstance(target, dict) else target
                    if isinstance(source_id, list):
                        source_id = source_id[0] if source_id else None
                    if isinstance(target_id, list):
                        target_id = target_id[0] if target_id else None
                    if not source_id or not target_id:
                        continue
                    relation_type = obj.get(_RELATION_TYPE_KEY, "")
                    if isinstance(relation_type, list):
                        relation_type = relation_type[0] if relation_type else ""
                    edge_key = (source_id, target_id, relation_type)
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        edges.append(
                            {
                                "source": source_id,
                                "target": target_id,
                                "type": relation_type,
                            }
                        )
                    continue
                nid = obj.get("id", [None])
                if isinstance(nid, list):
                    nid = nid[0] if nid else None
                if nid and nid != center_id and nid not in seen_nodes:
                    seen_nodes.add(nid)
                    neighbors.append(_unpack_node(self._parse_valuemap(obj)))

        return {"core_node": core_node, "neighbors": neighbors, "edges": edges}

    def get_context_chain(
        self, id: str, type: str = "FOLLOWS", user_name: str | None = None
    ) -> list[str]:
        tenant = self._tenant(user_name)
        tenant_literal = self._gremlin_literal(tenant)
        q = (
            f"g.V({self._gremlin_literal(id)}).has('user_name', {tenant_literal})"
            f".repeat(outE('{_EDGE_LABEL}').has('user_name', {tenant_literal})"
            f".has('{_RELATION_TYPE_KEY}', {self._gremlin_literal(type)})"
            f".inV().has('user_name', {tenant_literal}).simplePath())"
            f".emit().values('id')"
        )
        return self._gremlin_exec(q)

    # ------------------------------------------------------------------
    # Search / Recall
    # ------------------------------------------------------------------

    def search_by_embedding(
        self,
        vector: list[float],
        top_k: int = 5,
        scope: str | None = None,
        status: str | None = None,
        threshold: float | None = None,
        search_filter: dict | None = None,
        user_name: str | None = None,
        filter: dict | None = None,
        knowledgebase_ids: list[str] | None = None,
        return_fields: list[str] | None = None,
        **kwargs,
    ) -> list[dict]:
        """Vector recall with the Neo4j community backend's parameter semantics.

        ``search_filter`` narrows inside the vector store; ``filter`` and
        ``knowledgebase_ids`` post-filter the vector candidates against the
        graph (kb ids act as additional user names, ORed with the tenant);
        ``threshold`` keeps scoreless hits like the baseline does; and
        ``return_fields`` is validated before hydration.
        """
        # cube_name scopes the whole recall — the vector payload AND the
        # graph-side hydration below — matching the fulltext path.
        tenant = kwargs.get("cube_name") or self._tenant(user_name)
        payload_filter = {"user_name": tenant, "vector_sync": "success"}
        if scope:
            payload_filter["memory_type"] = scope
        if status:
            payload_filter["status"] = status
        if search_filter:
            payload_filter.update(search_filter)

        raw = self._vec_search(vector, top_k=top_k, payload_filter=payload_filter)
        scored_ids = []
        for item in raw:
            score = item.score if hasattr(item, "score") else item.get("score", 0.0)
            item_id = item.id if hasattr(item, "id") else item["id"]
            if threshold is not None and score is not None and score < threshold:
                continue
            scored_ids.append((item_id, score))

        if not filter and not knowledgebase_ids:
            return self._hydrate_search_results(scored_ids, return_fields, tenant)

        candidates = [item_id for item_id, _ in scored_ids]
        nodes = self._fetch_raw_nodes(candidates)
        allowed_users = {tenant, *(k for k in (knowledgebase_ids or []) if isinstance(k, str))}

        results = []
        for item_id, score in scored_ids:
            node = nodes.get(str(item_id))
            if node is None:
                continue
            if node.get("metadata", {}).get("user_name") not in allowed_users:
                continue
            if not self._match_graph_filter(node, filter):
                continue
            results.append({"id": item_id, "score": score})
        return self._hydrate_search_results(
            [(r["id"], r["score"]) for r in results], return_fields, tenant, nodes=nodes
        )

    def _hydrate_search_results(
        self,
        scored_ids: list[tuple[str, float | None]],
        return_fields: list[str] | None,
        user_name: str | None,
        nodes: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict]:
        """Attach the requested (validated) fields, preserving vector order."""
        if not return_fields:
            return [{"id": item_id, "score": score} for item_id, score in scored_ids]
        validated = self._validate_return_fields(return_fields)
        if nodes is None:
            nodes = {}
            if scored_ids:
                fetched = self.get_nodes(
                    [item_id for item_id, _ in scored_ids],
                    include_embedding=False,
                    user_name=user_name,
                )
                nodes = {node["id"]: node for node in fetched}
        results = []
        for item_id, score in scored_ids:
            entry: dict[str, Any] = {"id": item_id, "score": score}
            node = nodes.get(str(item_id))
            if node:
                if "memory" in validated:
                    entry["memory"] = node.get("memory")
                metadata = node.get("metadata", {})
                for field in validated:
                    if field not in ("id", "memory") and field in metadata:
                        entry[field] = metadata[field]
            results.append(entry)
        return results

    def _fetch_raw_nodes(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch nodes by id without the tenant restriction.

        knowledgebase-scoped recall must see nodes owned by the kb users, so
        the tenant filter in get_nodes() cannot be used here.
        """
        if not ids:
            return {}
        nodes: dict[str, dict[str, Any]] = {}
        for v in self._g.getVerticesById(list(ids)) or []:
            props = getattr(v, "properties", None)
            if isinstance(props, dict) and props.get("id"):
                node = _unpack_node(props)
                nodes[str(node["id"])] = node
        return nodes

    def _match_graph_filter(self, node: dict[str, Any], filter: dict | None) -> bool:
        """Evaluate the ``{"and"/"or": [...]}`` filter format against a node.

        Same semantics as the Neo4j backend's _build_filter_conditions_cypher:
        one condition maps field names to a value (equality) or a dict of
        operators (gt/lt/gte/lte/contains/in/like); keys within a condition are
        ANDed, "or" groups are ORed, a bare dict is one condition, and fields
        resolve against the node metadata. Unlike the Cypher builder, an
        unknown operator raises instead of being silently dropped.
        """
        if not filter:
            return True
        metadata = node.get("metadata", {})

        def field_value(key: str) -> Any:
            if key in metadata:
                return metadata[key]
            return node.get(key)

        def comparable(key: str, actual: Any, expected: Any) -> tuple[Any, Any] | None:
            if (
                key in ("created_at", "updated_at", "chat_time") or key.endswith("_at")
            ) and isinstance(actual, str) and isinstance(expected, str):
                actual_iso = normalize_to_utc_iso(actual)
                expected_iso = normalize_to_utc_iso(expected)
                if actual_iso is None or expected_iso is None:
                    return None
                return actual_iso, expected_iso
            return actual, expected

        def match_condition(condition: dict) -> bool:
            for key, value in condition.items():
                actual = field_value(key)
                if isinstance(value, dict):
                    for op, op_value in value.items():
                        if op in ("gt", "lt", "gte", "lte"):
                            pair = comparable(key, actual, op_value)
                            if pair is None:
                                return False
                            left, right = pair
                            try:
                                ok = {
                                    "gt": left > right,
                                    "lt": left < right,
                                    "gte": left >= right,
                                    "lte": left <= right,
                                }[op]
                            except TypeError:
                                return False
                        elif op == "contains":
                            if isinstance(actual, list | tuple | set):
                                ok = op_value in actual
                            elif isinstance(actual, str):
                                ok = str(op_value) in actual
                            else:
                                ok = False
                        elif op == "in":
                            if not isinstance(op_value, list):
                                raise ValueError(
                                    f"in operator only supports array format "
                                    f"(use {{'{key}': {{'in': [...]}}}})"
                                )
                            ok = actual in op_value
                        elif op == "like":
                            ok = str(op_value) in str(actual) if actual is not None else False
                        else:
                            raise ValueError(f"Unsupported filter operator: {op}")
                        if not ok:
                            return False
                elif actual != value:
                    return False
            return True

        def match_node(node: dict) -> bool:
            if "or" in node:
                conditions = [c for c in node["or"] if isinstance(c, dict)]
                return any(match_node(c) for c in conditions) if conditions else True
            if "and" in node:
                conditions = [c for c in node["and"] if isinstance(c, dict)]
                return all(match_node(c) for c in conditions)
            return match_condition(node)

        return match_node(filter)

    def search_by_fulltext(
        self,
        query_words: list[str],
        top_k: int = 10,
        scope: str | None = None,
        status: str | None = None,
        threshold: float | None = None,
        search_filter: dict | None = None,
        user_name: str | None = None,
        filter: dict | None = None,
        knowledgebase_ids: list[str] | None = None,
        tsquery_config: str | None = None,
        return_fields: list[str] | None = None,
        **kwargs,
    ) -> list[dict]:
        """Keyword recall over the memory text.

        Deliberately stronger than the Neo4j community backend, whose
        search_by_fulltext returns [] (its own TODO): keyword recall works
        here instead of silently dying, which changes recall results and is
        recorded as a divergence in .goal-task/hugegraph-neo4j-parity/design.md
        (D3). Multi-word queries OR the words and status defaults to
        'activated', both matching the PolarDB backend's implementation; the
        match score is a constant 1.0 because Text.contains carries no rank.
        ``cube_name`` scopes the query the same way the vector path treats it.
        """
        if tsquery_config:
            raise ValueError(
                "tsquery_config is a PostgreSQL full-text option; "
                "HugeGraph keyword recall has no equivalent"
            )
        words = [w for w in (query_words or []) if w and w.strip()]
        if not words:
            return []
        tenant = kwargs.get("cube_name") or self._tenant(user_name)
        effective_status = status if status else "activated"

        word_predicates = ", ".join(
            f"__.has('memory', Text.contains({self._gremlin_literal(w)}))" for w in words
        )
        if knowledgebase_ids:
            user_predicates = ", ".join(
                f"__.has('user_name', {self._gremlin_literal(u)})"
                for u in {tenant, *(k for k in knowledgebase_ids if isinstance(k, str))}
            )
            user_clause = f".or({user_predicates})"
        else:
            user_clause = f".has('user_name', {self._gremlin_literal(tenant)})"

        q = (
            f"g.V().hasLabel('{_VERTEX_LABEL}')"
            f"{user_clause}"
            f".or({word_predicates})"
            f".has('status', {self._gremlin_literal(effective_status)})"
        )
        if scope:
            q += f".has('memory_type', {self._gremlin_literal(scope)})"
        # Live 1.8.0 HStore emits a vertex once per matching or() branch, so
        # dedup server-side to keep the limit budget, and again client-side
        # below because replica behavior on this engine has been untrustworthy.
        q += f".dedup().limit({max(0, int(top_k))}).valueMap(true)"

        allowed_users = {tenant, *(k for k in (knowledgebase_ids or []) if isinstance(k, str))}
        scored_ids: list[tuple[str, float | None]] = []
        seen: set[str] = set()
        nodes: dict[str, dict[str, Any]] = {}
        for row in self._gremlin_exec(q):
            node = _unpack_node(self._parse_valuemap(row))
            if node["id"] in seen:
                continue
            if node.get("metadata", {}).get("user_name") not in allowed_users:
                continue
            if search_filter and not self._match_graph_filter(node, search_filter):
                continue
            if not self._match_graph_filter(node, filter):
                continue
            if threshold is not None and threshold > 1.0:
                continue
            seen.add(node["id"])
            nodes[node["id"]] = node
            scored_ids.append((node["id"], 1.0))
            if len(scored_ids) >= top_k:
                break
        # Keyword hits score a constant 1.0; hydration shares the vector
        # path's return_fields contract.
        return self._hydrate_search_results(scored_ids, return_fields, tenant, nodes=nodes)

    def get_by_metadata(
        self,
        filters: list[dict[str, Any]],
        user_name: str | None = None,
        filter: dict | None = None,
        knowledgebase_ids: list[str] | None = None,
        user_name_flag: bool = True,
        status: str | None = None,
    ) -> list[str]:
        """Retrieve node IDs whose metadata match every filter (AND logic).

        knowledgebase_ids act as additional user_names (OR), and the
        and/or-style ``filter`` dict post-filters the candidates, both with
        the baseline semantics. Schema-key predicates push into the Gremlin
        traversal (see _METADATA_PUSHDOWN_OPS); metadata_json-resident fields
        (key/tags/task_id/…) have no HugeGraph property to index and are
        evaluated client-side, their cost bounded by whatever pushed down.
        ``user_name_flag`` is accepted for signature parity — the Neo4j
        community baseline declares it and never reads it either.
        """
        normalized: list[dict[str, Any]] = []
        for f in filters or []:
            value = f.get("value")
            if isinstance(value, datetime):
                value = value.isoformat()
            normalized.append({**f, "value": value})
        # `in []` matches nothing on the baseline (IN []); skip the scan.
        if any(f.get("op") == "in" and not f.get("value") for f in normalized):
            return []

        tenant = self._tenant(user_name)
        if knowledgebase_ids:
            user_predicates = ", ".join(
                f"__.has('user_name', {self._gremlin_literal(u)})"
                for u in {tenant, *(k for k in knowledgebase_ids if isinstance(k, str))}
            )
            q = f"g.V().hasLabel('{_VERTEX_LABEL}').or({user_predicates}).dedup()"
        else:
            q = (
                f"g.V().hasLabel('{_VERTEX_LABEL}')"
                f".has('user_name', {self._gremlin_literal(tenant)})"
            )
        if status:
            q += f".has('status', {self._gremlin_literal(status)})"

        # Each filter contributes a list of alternative fragments: an `in`
        # with more than _GREMLIN_INDEX_CONDITION_LIMIT values must split into
        # one within() chunk per query (the cap is per within() clause —
        # live-probed: 100 OK, 101 → "Too many conditions(101) each query",
        # while two within(100) clauses coexist fine). Keep the cartesian
        # product shallow: three multi-value indexed conditions in one query
        # hit live server-side timeouts, and the product size grows with the
        # chunk count of every `in` filter. Every filter is re-checked
        # client-side below, so merging the rows with an id dedup is safe.
        variants: list[list[str]] = []
        for f in normalized:
            field = f["field"]
            op = f.get("op", "=")
            value = f["value"]
            if op not in _METADATA_PUSHDOWN_OPS and op not in ("starts_with", "ends_with"):
                raise ValueError(f"Unsupported metadata operator: {op}")
            # Pushdown value-type rules: `in` needs a list for within(),
            # `contains`/range predicates compare TEXT properties, so list
            # and non-string values stay client-side with the same result.
            if field in _SCHEMA_MEMORY_PROPERTIES and field not in _METADATA_CLIENT_ONLY_FIELDS:
                name = self._property_name(field)
                if op == "=":
                    variants.append([f".has('{name}', {self._gremlin_literal(value)})"])
                elif op == "in" and isinstance(value, list | tuple | set):
                    variants.append(
                        [
                            ".has('{name}', within({values}))".format(
                                name=name,
                                values=", ".join(self._gremlin_literal(v) for v in chunk),
                            )
                            for chunk in self._chunked(
                                list(value), self._GREMLIN_INDEX_CONDITION_LIMIT
                            )
                        ]
                    )
                elif op == "contains" and isinstance(value, str):
                    variants.append(
                        [f".has('{name}', Text.contains({self._gremlin_literal(value)}))"]
                    )
                elif op in _GREMLIN_RANGE_PREDICATES and isinstance(value, str):
                    predicate = _GREMLIN_RANGE_PREDICATES[op]
                    variants.append(
                        [f".has('{name}', {predicate}({self._gremlin_literal(value)}))"]
                    )
                else:
                    variants.append([""])
            else:
                variants.append([""])

        def matches(actual: Any, op: str, expected: Any) -> bool:
            if op == "=":
                return actual == expected
            if op == "in":
                return actual in expected if isinstance(expected, list | tuple | set) else False
            if op == "contains":
                if isinstance(actual, list | tuple | set):
                    candidates = (
                        expected if isinstance(expected, list | tuple | set) else [expected]
                    )
                    return bool(set(actual) & set(candidates))
                return (
                    expected in actual
                    if isinstance(actual, str) and isinstance(expected, str)
                    else False
                )
            if op == "starts_with":
                return (
                    isinstance(actual, str)
                    and isinstance(expected, str)
                    and actual.startswith(expected)
                )
            if op == "ends_with":
                return (
                    isinstance(actual, str)
                    and isinstance(expected, str)
                    and actual.endswith(expected)
                )
            try:
                if op == ">":
                    return actual > expected
                if op == ">=":
                    return actual >= expected
                if op == "<":
                    return actual < expected
                return actual <= expected
            except TypeError:
                return False

        result = []
        seen: set[str] = set()
        for combo in itertools.product(*variants):
            for row in self._gremlin_exec(q + "".join(combo) + ".valueMap(true)"):
                node = _unpack_node(self._parse_valuemap(row))
                metadata = node["metadata"]
                if not self._match_graph_filter(node, filter):
                    continue
                if all(
                    matches(
                        metadata[f["field"]] if f["field"] in metadata else node.get(f["field"]),
                        f.get("op", "="),
                        f["value"],
                    )
                    for f in normalized
                ):
                    node_id = str(node["id"])
                    if node_id not in seen:
                        seen.add(node_id)
                        result.append(node_id)
        return result

    def list_user_names(self) -> list[str]:
        """Return the distinct ``user_name`` values on Memory nodes.

        The baseline (neo4j.py) does the same: background maintenance cannot
        rely on ``config.user_name`` matching the identities that actually
        own data. Rows without a user_name are skipped rather than returned
        as None entries.
        """
        rows = self._gremlin_exec(f"g.V().hasLabel('{_VERTEX_LABEL}').values('user_name').dedup()")
        return sorted({r for r in rows if isinstance(r, str) and r})

    def get_memory_count(self, memory_type: str, user_name: str | None = None) -> int:
        tenant = self._tenant(user_name)
        q = (
            f"g.V().hasLabel('{_VERTEX_LABEL}')"
            f".has('memory_type', {self._gremlin_literal(memory_type)})"
            f".has('user_name', {self._gremlin_literal(tenant)}).count()"
        )
        rows = self._gremlin_exec(q)
        return int(rows[0]) if rows else 0

    def node_not_exist(self, scope: str, user_name: str | None = None) -> bool:
        return self.get_memory_count(scope, user_name) == 0

    def remove_oldest_memory(
        self, memory_type: str, keep_latest: int, user_name: str | None = None
    ) -> None:
        tenant = self._tenant(user_name)
        keep_latest = max(0, int(keep_latest))
        traversal = (
            f"g.V().hasLabel('{_VERTEX_LABEL}')"
            f".has('memory_type', {self._gremlin_literal(memory_type)})"
            f".has('user_name', {self._gremlin_literal(tenant)})"
            f".order().by('updated_at', desc)"
            f".range({keep_latest}, -1)"
        )
        self._delete_matching_vertices(traversal)

    def get_all_memory_items(
        self,
        scope: str,
        include_embedding: bool = False,
        user_name: str | None = None,
        status: str | None = None,
        filter: dict | None = None,
        knowledgebase_ids: list[str] | None = None,
        **kwargs,
    ) -> list[dict]:
        """Export every memory item under one memory_type scope.

        knowledgebase_ids act as additional user_names (OR) and the
        and/or-style ``filter`` dict post-filters the candidates, both with
        the baseline semantics; scope validation matches the baseline too.

        Recorded divergence: ``status`` filters here, but the Neo4j community
        baseline has no status parameter, so the caller's status="activated"
        (retrieve/recall.py) lands in its **kwargs and is silently ignored
        there. This backend honours what the caller explicitly asks for.
        """
        if scope not in ("WorkingMemory", "LongTermMemory", "UserMemory", "OuterMemory"):
            raise ValueError(f"Unsupported memory type scope: {scope}")
        tenant = self._tenant(user_name)
        if knowledgebase_ids:
            user_predicates = ", ".join(
                f"__.has('user_name', {self._gremlin_literal(u)})"
                for u in {tenant, *(k for k in knowledgebase_ids if isinstance(k, str))}
            )
            q = f"g.V().hasLabel('{_VERTEX_LABEL}').or({user_predicates}).dedup()"
        else:
            q = (
                f"g.V().hasLabel('{_VERTEX_LABEL}')"
                f".has('user_name', {self._gremlin_literal(tenant)})"
            )
        q += f".has('memory_type', {self._gremlin_literal(scope)})"
        if status:
            q += f".has('status', {self._gremlin_literal(status)})"
        q += ".valueMap(true)"

        result = []
        for row in self._gremlin_exec(q):
            node = _unpack_node(self._parse_valuemap(row), include_embedding)
            if self._match_graph_filter(node, filter):
                result.append(node)
        return result

    def get_structure_optimization_candidates(
        self, scope: str, include_embedding: bool = False, **kwargs
    ) -> list[dict]:
        """Isolated nodes within one scope, with the baseline's definition:
        activated nodes carrying no PARENT relation in either direction. The
        baseline hardcodes the same 'activated' status. include_embedding is
        swallowed by the baseline (kwargs) and honoured here, like status in
        get_all_memory_items."""
        tenant = self._tenant(kwargs.get("user_name"))
        q = (
            f"g.V().hasLabel('{_VERTEX_LABEL}')"
            f".has('memory_type', {self._gremlin_literal(scope)})"
            f".has('status', 'activated')"
            f".has('user_name', {self._gremlin_literal(tenant)})"
            # The baseline counts only PARENT edges; relation_type carries the
            # edge type on this backend (design.md D6).
            f".filter(bothE('{_EDGE_LABEL}')"
            f".has('{_RELATION_TYPE_KEY}', 'PARENT').count().is(0))"
            ".valueMap(true)"
        )
        rows = self._gremlin_exec(q)
        return [_unpack_node(self._parse_valuemap(r), include_embedding) for r in rows]

    def get_grouped_counts(
        self,
        group_fields: list[str],
        where_clause: str = "",
        params: dict[str, Any] | None = None,
        user_name: str | None = None,
    ) -> list[dict]:
        if not group_fields:
            raise ValueError("group_fields cannot be empty")
        # The baseline's where_clause is a raw Cypher fragment; silently
        # dropping it would return unfiltered counts. There is no gremlin
        # counterpart for an arbitrary Cypher string, so reject it loudly.
        if where_clause or params:
            raise ValueError(
                "get_grouped_counts where_clause/params are Cypher fragments "
                "with no HugeGraph counterpart; pass group_fields/user_name only"
            )
        tenant = self._tenant(user_name)
        fields = [self._property_name(field) for field in group_fields]
        traversal = (
            f"g.V().hasLabel('{_VERTEX_LABEL}').has('user_name', {self._gremlin_literal(tenant)})"
        )
        if len(fields) == 1:
            rows = self._gremlin_exec(
                traversal + f".groupCount().by(values({self._gremlin_literal(fields[0])}))"
            )
            if rows and isinstance(rows[0], dict):
                return [{fields[0]: value, "count": int(count)} for value, count in rows[0].items()]
            return []

        rows = self._gremlin_exec(
            traversal + f".valueMap({', '.join(self._gremlin_literal(field) for field in fields)})"
        )
        grouped: dict[tuple[Any, ...], int] = {}
        for row in rows:
            values = self._parse_valuemap(row)
            key = tuple(values.get(field) for field in fields)
            grouped[key] = grouped.get(key, 0) + 1
        return [
            {**dict(zip(fields, key, strict=True)), "count": count}
            for key, count in grouped.items()
        ]

    def get_neighbors_by_tag(
        self,
        tags: list[str],
        exclude_ids: list[str],
        top_k: int = 5,
        min_overlap: int = 1,
        user_name: str | None = None,
    ) -> list[dict[str, Any]]:
        if top_k <= 0 or not tags:
            return []
        tenant = self._tenant(user_name)
        scan_batch_size = 1000
        required_overlap = max(1, int(min_overlap))
        tag_set = set(tags)
        ranked: list[tuple[int, int, dict[str, Any]]] = []
        sequence = 0
        # Exclusion stays client-side like the baseline's WHERE NOT IN: a
        # server-side .not(hasId(*ids)) hits the Groovy parameter cap and
        # chunked exclusion queries leak excluded ids across chunks, while
        # the paged scan covers the whole tenant anyway.
        exclude_set = {str(e) for e in exclude_ids}
        base_query = (
            f"g.V().hasLabel('{_VERTEX_LABEL}')"
            f".has('user_name', {self._gremlin_literal(tenant)})"
            f".has('status', 'activated')"
        )

        # Tags live in metadata_json, so HugeGraph cannot apply the overlap
        # predicate through the fixed schema. Scan bounded pages and keep only
        # the best top_k candidates to preserve correctness without materializing
        # the whole tenant graph in Python.
        offset = 0
        while True:
            q = f"{base_query}.range({offset}, {offset + scan_batch_size}).valueMap(true)"
            rows = self._gremlin_exec(q)
            if not rows:
                break
            for row in rows:
                props = self._parse_valuemap(row)
                node = _unpack_node(props)
                if node["id"] in exclude_set:
                    continue
                metadata = node.get("metadata", {})
                if (
                    metadata.get("memory_type") == "WorkingMemory"
                    or metadata.get("type") == "reasoning"
                ):
                    continue
                node_tags = metadata.get("tags", [])
                if isinstance(node_tags, str):
                    node_tags = [node_tags]
                overlap = len(tag_set & set(node_tags))
                if overlap < required_overlap:
                    continue
                node["overlap_count"] = overlap
                candidate = (overlap, sequence, node)
                sequence += 1
                if len(ranked) < top_k:
                    heapq.heappush(ranked, candidate)
                elif (overlap, candidate[1]) > (ranked[0][0], ranked[0][1]):
                    heapq.heapreplace(ranked, candidate)
            offset += len(rows)
            if len(rows) < scan_batch_size:
                break

        ranked.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        return [item[2] for item in ranked]

    def get_children_with_embeddings(
        self, id: str, user_name: str | None = None
    ) -> list[dict[str, Any]]:
        tenant = self._tenant(user_name)
        q = (
            f"g.V({self._gremlin_literal(id)}).outE('{_EDGE_LABEL}')"
            f".has('{_RELATION_TYPE_KEY}', 'PARENT')"
            f".has('user_name', {self._gremlin_literal(tenant)})"
            f".inV().has('user_name', {self._gremlin_literal(tenant)}).valueMap(true)"
        )
        rows = self._gremlin_exec(q)
        result = []
        for row in rows:
            props = self._parse_valuemap(row)
            embedding = None
            if props.get("embedding_json"):
                with suppress(TypeError, json.JSONDecodeError):
                    embedding = json.loads(props["embedding_json"])
            result.append(
                {
                    "id": props.get("id", ""),
                    "memory": props.get("memory", ""),
                    "embedding": embedding,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def clear(self, user_name: str | None = None) -> None:
        tenant = self._tenant(user_name)
        prefix = (
            f"g.V().hasLabel('{_VERTEX_LABEL}').has('user_name', {self._gremlin_literal(tenant)})"
        )
        self._delete_matching_vertices(prefix)

    def export_graph(
        self,
        page: int | None = None,
        page_size: int | None = None,
        memory_type: list[str] | None = None,
        status: list[str] | None = None,
        filter: dict | None = None,
        include_embedding: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        """Export nodes and edges in the Neo4j baseline's structured form.

        Pagination (both page and page_size given; the baseline clamps
        page<1 to 1 and page_size<1 to 10) applies to nodes and edges alike,
        and total_nodes/total_edges count the filtered set before
        pagination. status=None excludes 'deleted' (the baseline default);
        an explicit empty list imposes no status constraint, like the
        baseline's fall-through. The and/or ``filter`` dict applies to nodes
        and to both edge endpoints. Without a ``filter`` dict the pagination
        and counts happen server-side (range + count, endpoint conditions
        pushed into the edge traversal); with one, the and/or conditions are
        evaluated client-side against the fetched node set — gremlin has no
        server-side join — so that path fetches the full matching set once
        (admin/dump traffic, the documented cost).
        """
        tenant = self._tenant(kwargs.get("user_name"))
        tenant_literal = self._gremlin_literal(tenant)
        use_pagination = page is not None and page_size is not None
        if use_pagination:
            page = max(page, 1)
            page_size = 10 if page_size < 1 else page_size
            skip = (page - 1) * page_size

        conditions = f".has('user_name', {tenant_literal})"
        if memory_type:
            if isinstance(memory_type, str):
                conditions += f".has('memory_type', {self._gremlin_literal(memory_type)})"
            else:
                conditions += self._split_within_clause("memory_type", memory_type)
        # status=None excludes 'deleted'. The engine rejects neq() on
        # non-indexed properties (live-probed), so exclusion uses not().
        if status is None:
            conditions += ".not(__.has('status', 'deleted'))"
        elif status:
            conditions += self._split_within_clause("status", status)

        edge_query = (
            f"g.V().hasLabel('{_VERTEX_LABEL}'){conditions}"
            f".outE('{_EDGE_LABEL}').has('user_name', {tenant_literal})"
            # Both edge endpoints must satisfy the same node conditions; the
            # live-probed where(__.inV()...) keeps the project on the edge.
            f".where(__.inV(){conditions})"
            ".project('source','target','type','a_created','b_created')"
            ".by(outV().values('id')).by(inV().values('id')).by('relation_type')"
            ".by(outV().values('created_at')).by(inV().values('created_at'))"
        )
        # The baseline orders by a.created_at/b.created_at/a.id/b.id, DESC.
        edge_order = (
            ".order().by('a_created', desc).by('b_created', desc)"
            ".by('source', desc).by('target', desc)"
        )

        if not filter:
            # Server-side pagination and counts (the hot path — tree.py's
            # product get_all pages through here).
            total_nodes = int(
                (
                    self._gremlin_exec(f"g.V().hasLabel('{_VERTEX_LABEL}'){conditions}.count()")
                    or [0]
                )[0]
            )
            total_edges = int((self._gremlin_exec(edge_query + ".count()") or [0])[0])
            node_page = f".range({skip}, {skip + page_size})" if use_pagination else ""
            all_nodes = [
                _unpack_node(self._parse_valuemap(r), include_embedding)
                for r in self._gremlin_exec(
                    f"g.V().hasLabel('{_VERTEX_LABEL}'){conditions}"
                    f".order().by('created_at', desc).by('id', desc){node_page}.valueMap(true)"
                )
            ]
            edge_page = f".range({skip}, {skip + page_size})" if use_pagination else ""
            raw_edges = self._gremlin_exec(edge_query + edge_order + edge_page)
            edges = [
                {"source": r.get("source"), "target": r.get("target"), "type": r.get("type")}
                for r in raw_edges
            ]
            return {
                "nodes": all_nodes,
                "edges": edges,
                "total_nodes": total_nodes,
                "total_edges": total_edges,
            }

        # filter present: evaluate the and/or conditions client-side against
        # the fetched node set (gremlin has no server-side join).
        all_nodes = []
        for r in self._gremlin_exec(
            f"g.V().hasLabel('{_VERTEX_LABEL}'){conditions}"
            ".order().by('created_at', desc).by('id', desc).valueMap(true)"
        ):
            node = _unpack_node(self._parse_valuemap(r), include_embedding)
            if self._match_graph_filter(node, filter):
                all_nodes.append(node)
        by_id = {n["id"]: n for n in all_nodes}

        decorated: list[tuple[str, str, str, str, str]] = []
        for row in self._gremlin_exec(edge_query):
            source, target = row.get("source"), row.get("target")
            source_node, target_node = by_id.get(source), by_id.get(target)
            if source_node is None or target_node is None:
                continue  # an endpoint is outside the filtered node set
            if not (
                self._match_graph_filter(source_node, filter)
                and self._match_graph_filter(target_node, filter)
            ):
                continue
            decorated.append(
                (
                    row.get("a_created") or "",
                    row.get("b_created") or "",
                    source,
                    target,
                    row.get("type") or "",
                )
            )
        decorated.sort(reverse=True)
        edges = [
            {"source": source, "target": target, "type": edge_type}
            for _, _, source, target, edge_type in decorated
        ]

        return {
            "nodes": all_nodes[skip : skip + page_size] if use_pagination else all_nodes,
            "edges": edges[skip : skip + page_size] if use_pagination else edges,
            "total_nodes": len(all_nodes),
            "total_edges": len(edges),
        }

    def import_graph(self, data: dict[str, Any], user_name: str | None = None) -> None:
        nodes = data.get("nodes", [])
        edges = data.get("edges", [])
        if nodes:
            self.add_nodes_batch(nodes, user_name)
        for edge in edges:
            source = edge.get("source", edge.get("from"))
            target = edge.get("target", edge.get("to"))
            if source is None or target is None:
                raise ValueError("Imported edge requires source/target or from/to")
            self.add_edge(source, target, edge["type"], user_name)

    # TODO(hugegraph): the three structure-maintenance methods below are part of
    # the BaseGraphDB contract but are not implemented for this backend. No
    # production path calls them today, so the stubs are inert; they must be
    # implemented (or the interface narrowed) before any structure
    # optimization / dedup flow is enabled for HugeGraph.
    def deduplicate_nodes(self) -> None:
        raise NotImplementedError

    def detect_conflicts(self) -> list[tuple[str, str]]:
        raise NotImplementedError

    def merge_nodes(self, id1: str, id2: str) -> str:
        raise NotImplementedError

    def delete_node_by_prams(
        self,
        writable_cube_ids: list[str] | None = None,
        memory_ids: list[str] | None = None,
        file_ids: list[str] | None = None,
        filter: dict[str, Any] | None = None,
    ) -> int:
        # Contract parity with neo4j_community.py:947: file_ids deletion must
        # be scoped by writable_cube_ids (raised, not silently scoped to the
        # default tenant), memory_ids alone delete globally by id, and the
        # condition groups OR together (any of memory_ids / file_ids /
        # filter can match).
        if file_ids and (not writable_cube_ids or len(writable_cube_ids) == 0):
            raise ValueError("writable_cube_ids is required when deleting by file_ids")
        if not memory_ids and not file_ids and not filter:
            logger.warning(
                "[delete_node_by_prams] No nodes to delete "
                "(no memory_ids, file_ids, or filter provided)"
            )
            return 0

        # filter resolves through get_by_metadata with the baseline's exact
        # arguments (empty filters, no explicit user, cubes as kb scope).
        filter_ids = (
            self.get_by_metadata(
                filters=[],
                user_name=None,
                filter=filter,
                knowledgebase_ids=writable_cube_ids,
            )
            if filter
            else []
        )
        scoped_ids = {*(i for i in (memory_ids or []) if isinstance(i, str)), *filter_ids}
        if not scoped_ids and not file_ids:
            # Baseline parity: with no id conditions left (filter resolved to
            # nothing, no memory_ids) and no file_ids, the baseline's WHERE
            # is empty and deletes nothing — do not scan the whole graph for 0.
            logger.warning(
                "[delete_node_by_prams] No nodes to delete "
                "(filter matched nothing and no memory_ids/file_ids given)"
            )
            return 0

        # Cube scope ANDs with the data conditions; without cubes the id
        # conditions alone decide (global delete by id). hasId narrowing is
        # skipped when file_ids are given: they are an independent OR branch,
        # and narrowing would make file-only matches unreachable. Both
        # within() forms count list elements toward the Groovy parameter
        # cap, so chunk the cube and id lists independently and merge rows.
        probe = f"g.V().hasLabel('{_VERTEX_LABEL}')"
        str_cubes = (
            [t for t in writable_cube_ids if isinstance(t, str)] if writable_cube_ids else []
        )
        if writable_cube_ids and not str_cubes:
            raise ValueError(
                "writable_cube_ids must contain str members; got "
                f"{type(next(iter(writable_cube_ids))).__name__} only"
            )
        # Each cube chunk adds one indexed within() condition, so chunk against
        # the tighter index-condition cap (100), not the Groovy parameter cap.
        cube_chunks = (
            self._chunked(str_cubes, self._GREMLIN_INDEX_CONDITION_LIMIT) if str_cubes else [[]]
        )
        id_chunks = self._chunked(sorted(scoped_ids)) if scoped_ids and not file_ids else [[]]
        # Selection only needs the id and the file_ids inside metadata_json —
        # projecting both keeps whole-cube scans (the file_ids branch) from
        # materializing every memory and embedding on the client.
        rows = []
        for cube_chunk in cube_chunks:
            cube_query = probe
            if cube_chunk:
                cube_groups = ", ".join(self._gremlin_literal(t) for t in cube_chunk)
                cube_query += f".has('user_name', within({cube_groups}))"
            for id_chunk in id_chunks:
                id_query = cube_query
                if id_chunk:
                    id_groups = ", ".join(self._gremlin_literal(i) for i in id_chunk)
                    id_query += f".hasId(within({id_groups}))"
                rows.extend(
                    self._gremlin_exec(
                        id_query + ".project('id', 'metadata_json').by('id').by('metadata_json')"
                    )
                )

        memory_id_set = {i for i in (memory_ids or []) if isinstance(i, str)}
        filter_id_set = {str(i) for i in filter_ids}
        selected_ids = []
        for row in rows:
            node = _unpack_node(self._parse_valuemap(row))
            node_id = str(node["id"])
            node_file_ids = node["metadata"].get("file_ids") or []
            hit_by_memory = node_id in memory_id_set
            hit_by_filter = node_id in filter_id_set
            hit_by_files = bool(file_ids) and all(f in node_file_ids for f in file_ids)
            if hit_by_memory or hit_by_filter or hit_by_files:
                selected_ids.append(node_id)

        if not selected_ids:
            return 0
        self._vec_delete(selected_ids)
        for chunk in self._chunked(selected_ids):
            id_groups = ", ".join(self._gremlin_literal(i) for i in chunk)
            self._gremlin_exec(f"g.V().hasId(within(*[{id_groups}])).drop()")
        return len(selected_ids)

    def get_user_names_by_memory_ids(self, memory_ids: list[str]) -> dict[str, str | None]:
        if not memory_ids:
            return {}
        result: dict[str, str | None] = dict.fromkeys(memory_ids)
        for chunk in self._chunked(list(memory_ids)):
            ids_str = self._gremlin_literal(chunk)
            q = f"g.V().hasId(*{ids_str}).project('id', 'user_name').by('id').by('user_name')"
            rows = self._gremlin_exec(q)
            for row in rows:
                if isinstance(row, dict):
                    result[row.get("id", "")] = row.get("user_name")
        return result

    def exist_user_name(self, user_name: str) -> dict[str, bool]:
        q = (
            f"g.V().hasLabel('{_VERTEX_LABEL}')"
            f".has('user_name', {self._gremlin_literal(user_name)}).limit(1).count()"
        )
        rows = self._gremlin_exec(q)
        return {user_name: bool(rows and rows[0])}

    def delete_node_by_mem_cube_id(
        self,
        mem_cube_id: str | None = None,
        delete_record_id: str | None = None,
        hard_delete: bool = False,
    ) -> int:
        if not mem_cube_id or not delete_record_id:
            return 0
        traversal = (
            f"g.V().hasLabel('{_VERTEX_LABEL}')"
            f".has('user_name', {self._gremlin_literal(mem_cube_id)})"
        )
        if hard_delete:
            # delete_record_id has no index, so this engine rejects an
            # equality condition on it even next to indexed user_name —
            # narrow server-side by user_name and filter client-side, the
            # same predicate as the baseline's WHERE. The projection keeps
            # memory bodies and embedding vectors off the wire.
            rows = self._gremlin_exec(
                traversal + ".project('id', 'delete_record_id').by('id').by('delete_record_id')"
            )
            selected = [
                row
                for row in rows
                if str(self._parse_valuemap(row).get("delete_record_id", "")) == delete_record_id
            ]
            return self._delete_selected_vertices(selected)

        # neq() is rejected on non-indexed properties; .not() probes clean.
        rows = self._gremlin_exec(traversal + ".not(__.has('status', 'deleted')).valueMap(true)")
        self._sync_vector_status(rows, mem_cube_id, "deleted")
        ids = [str(self._parse_valuemap(row).get("id", "")) for row in rows]
        if ids:
            now = _now_iso()
            for chunk in self._chunked(ids):
                id_groups = ", ".join(self._gremlin_literal(i) for i in chunk)
                self._gremlin_exec(
                    f"g.V().hasId(within(*[{id_groups}]))"
                    f".property('status', 'deleted')"
                    f".property('delete_record_id', {self._gremlin_literal(delete_record_id)})"
                    f".property('delete_time', {self._gremlin_literal(now)})"
                )
        return len(ids)

    def recover_memory_by_mem_cube_id(
        self,
        mem_cube_id: str | None = None,
        delete_record_id: str | None = None,
    ) -> int:
        if not mem_cube_id or not delete_record_id:
            return 0
        tenant = mem_cube_id
        # delete_record_id has no index, so narrow server-side by the indexed
        # properties and filter the record id client-side, like the baseline's
        # WHERE. The extra status='deleted' condition only skips recoveries
        # that would be no-ops anyway.
        traversal = (
            f"g.V().hasLabel('{_VERTEX_LABEL}')"
            f".has('user_name', {self._gremlin_literal(tenant)})"
            f".has('status', 'deleted')"
        )
        rows = self._gremlin_exec(
            traversal + ".project('id', 'delete_record_id').by('id').by('delete_record_id')"
        )
        ids = [
            str(self._parse_valuemap(row).get("id", ""))
            for row in rows
            if str(self._parse_valuemap(row).get("delete_record_id", "")) == delete_record_id
        ]
        if not ids:
            return 0
        if self._vector_index is not None:
            # _sync_vector_status needs the full property rows (embedding_json
            # and friends) to rebuild the vector payloads, so re-fetch the
            # matched ids; without a vector mirror there is nothing to sync.
            rows = []
            for chunk in self._chunked(ids):
                id_groups = ", ".join(self._gremlin_literal(i) for i in chunk)
                rows.extend(
                    self._gremlin_exec(f"g.V().hasId(within(*[{id_groups}])).valueMap(true)")
                )
            self._sync_vector_status(rows, tenant, "activated")
        for chunk in self._chunked(ids):
            id_groups = ", ".join(self._gremlin_literal(i) for i in chunk)
            self._gremlin_exec(
                f"g.V().hasId(within(*[{id_groups}]))"
                + ".property('status', 'activated')"
                + ".property('delete_record_id', '').property('delete_time', '')"
            )
        return len(ids)
