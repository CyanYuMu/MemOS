"""Cloud-compatible filter translation.

The cloud API documents a filter vocabulary (``features/filters.md``) that does
not match the open-source node properties one-to-one:

===========================  ==============================
cloud field                  open-source property
===========================  ==============================
``create_time``              ``created_at``
``update_time``              ``updated_at``
``related_id``               ``user_id`` / ``agent_id`` (flattened from ``info``)
``tags``                     ``tags`` (already matches)
===========================  ==============================

Cloud also allows per-source roots (``user`` / ``agent`` / ``public`` /
``knowledgebase``). Each root is routed to the memories of that owner: the
conditions are wrapped in ``owner_type = <source>`` so a condition written for
one source cannot match another source's memories. ``knowledgebase`` is still
rejected because this deployment has no knowledge-base memory entity; dropping
it silently would quietly widen the result set.

The output uses only ``and`` / ``or`` nodes plus leaf conditions, so it can be
consumed by both graph backends.
"""

import copy

from typing import Any


_FIELD_ALIASES = {
    "create_time": "created_at",
    "update_time": "updated_at",
}

_PER_SOURCE_KEYS = {"user", "agent", "public", "knowledgebase"}
_SUPPORTED_SOURCES = {"user", "agent", "public"}
_COMPARISON_OPERATORS = {
    "in",
    "nin",
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "not_contains",
    "startswith",
    "endswith",
}


def normalize_filter(raw_filter: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a filter using open-source field names, or raise ``ValueError``.

    The input is never mutated; callers can keep using the original object.
    """
    if raw_filter is None:
        return None
    if not isinstance(raw_filter, dict):
        raise ValueError("filter must be an object")  # noqa: TRY004

    return _normalize_root(copy.deepcopy(raw_filter))


def _normalize_root(node: dict[str, Any]) -> dict[str, Any] | None:
    if "and" in node or "or" in node:
        return _normalize_logic_node(node)

    source_keys = set(node) & _PER_SOURCE_KEYS
    if source_keys:
        if not set(node) <= _PER_SOURCE_KEYS:
            mixed = sorted(set(node) - _PER_SOURCE_KEYS)
            raise ValueError(
                "filter cannot mix per-source keys (user/agent/public/knowledgebase) "
                f"with other keys: {mixed}"
            )
        unsupported = sorted(set(node) - _SUPPORTED_SOURCES)
        if unsupported:
            raise ValueError(
                "filter sources are not supported by this deployment yet: "
                f"{unsupported}. Only 'user', 'agent' and 'public' are available."
            )
        branches: list[dict[str, Any]] = []
        for source, value in node.items():
            condition = _normalize_logic_node(value)
            gate = {"owner_type": source}
            if condition is None:
                branches.append(gate)
            elif set(condition) == {"and"}:
                branches.append({"and": [gate, *condition["and"]]})
            else:
                branches.append({"and": [gate, condition]})
        return branches[0] if len(branches) == 1 else {"or": branches}

    # Per-cube filters (developer API): {"cube_a": {"and": [...]}, ...}.
    # A bare condition such as {"scene": "chat"} is ambiguous with a cube map,
    # so only an all-logic-node map is treated as per-cube; everything else is
    # a legacy bare condition, which the graph backends have always accepted.
    if not node:
        return None
    if _looks_like_bare_condition(node):
        return _normalize_condition(node)
    normalized_per_cube: dict[str, Any] = {}
    for cube_id, value in node.items():
        if not isinstance(value, dict):
            raise ValueError(  # noqa: TRY004
                "filter root must be an object with 'and' or 'or' "
                "(cloud contract); nested per-cube filters are also accepted"
            )
        normalized_per_cube[cube_id] = _normalize_logic_node(value)
    return normalized_per_cube


def is_per_cube_map(node: dict[str, Any]) -> bool:
    """True when ``node`` maps cube ids to filters (legacy developer API)."""
    if not node or set(node) & (_PER_SOURCE_KEYS | {"and", "or"}):
        return False
    return not _looks_like_bare_condition(node)


def _looks_like_bare_condition(node: dict[str, Any]) -> bool:
    """Tell a legacy bare condition from a per-cube map.

    Per-cube maps are built by the developer API and every value is an
    ``and``/``or`` logic node. A condition uses scalar/array values (equality),
    comparison-operator objects, or cloud alias fields.
    """
    for key, value in node.items():
        if key in _FIELD_ALIASES or key == "related_id":
            return True
        if not isinstance(value, dict):
            return True
        if set(value) & _COMPARISON_OPERATORS:
            return True
    return False


def _normalize_logic_node(node: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        raise ValueError("each filter condition must be an object")  # noqa: TRY004

    if "and" in node or "or" in node:
        logic = "and" if "and" in node else "or"
        extra = sorted(set(node) - {logic})
        if extra:
            raise ValueError(f"filter '{logic}' cannot be combined with other keys: {extra}")
        children = node[logic]
        if not isinstance(children, list):
            raise ValueError(f"filter '{logic}' must be an array")
        normalized_children = []
        for child in children:
            normalized = _normalize_logic_node(child)
            if normalized:
                normalized_children.append(normalized)
        return {logic: normalized_children} if normalized_children else None

    if set(node) & _PER_SOURCE_KEYS:
        raise ValueError("per-source filters must be at the root of the filter object")

    return _normalize_condition(node)


def _normalize_condition(node: dict[str, Any]) -> dict[str, Any] | None:
    conditions: list[dict[str, Any]] = []
    for key, value in node.items():
        if key == "related_id":
            related = _related_id_condition(value)
            if related:
                conditions.append(related)
            continue
        if value is None:
            # `null` used to mean "never matches". The graph compiler now maps
            # None to `IS NULL`, which would let a caller select every legacy
            # node missing the property (e.g. owner_type) — reject it at the
            # API boundary; internal owner-scope filters bypass normalize_filter.
            raise ValueError(f"filter value for {key!r} cannot be null; omit the key instead")
        if isinstance(value, dict):
            null_ops = sorted(op for op, op_value in value.items() if op_value is None)
            if null_ops:
                raise ValueError(
                    f"filter operator value for {key!r} cannot be null: {', '.join(null_ops)}"
                )
        conditions.append({_FIELD_ALIASES.get(key, key): value})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"and": conditions}


def _related_id_condition(value: Any) -> dict[str, Any] | None:
    """``related_id`` matches memories related to any of the given user/agent IDs.

    Both IDs are stored as flattened properties (``info.user_id`` /
    ``info.agent_id``), so this expands to an ``or`` group.
    """
    if isinstance(value, str):
        ids = [value]
    elif isinstance(value, list | tuple | set):
        ids = [str(item) for item in value if item]
    else:
        raise ValueError("related_id must be a string or an array of strings")  # noqa: TRY004

    if not ids:
        return None

    branches = [
        {"user_id": {"in": ids}},
        {"agent_id": {"in": ids}},
    ]
    return {"or": branches}
