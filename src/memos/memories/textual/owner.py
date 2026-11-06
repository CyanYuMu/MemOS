"""Owner (subject) model for memory isolation.

Tenant isolation stays on ``user_name`` (the cube). Inside one tenant each
memory node carries exactly one owner:

* ``user``   – private memory of a participant (``owner_id``);
* ``agent``  – private memory of an agent / AI role (``owner_id``);
* ``group``  – conversation-shared memory of a group (``owner_id``);
* ``public`` – tenant-wide memory (``owner_id`` is the tenant).

The helpers here translate a request's ``user_id`` / ``agent_id`` /
``group_id`` / ``allow_public`` into per-owner write views, and translate a
read request into an owner scope filter. Both are additive: nodes keep the
``user_id`` / ``agent_id`` / ``group_id`` compatibility properties so existing
client ``filter`` expressions keep working.
"""

from __future__ import annotations

import hashlib
import os

from dataclasses import dataclass, field
from typing import Any, Literal


OWNER_TYPES: tuple[str, ...] = ("user", "agent", "group", "public")
MAX_OWNER_IDS = 20
DEFAULT_TENANT = "default"

#: ``allow_memory_view`` value -> graph ``memory_type`` values it produces.
MEMORY_VIEW_TYPES: dict[str, frozenset[str]] = {
    "detail_factual": frozenset({"WorkingMemory", "LongTermMemory", "UserMemory", "RawFileMemory"}),
    "preference": frozenset({"PreferenceMemory"}),
    "event": frozenset({"EventMemory"}),
    "tool_memory": frozenset({"ToolSchemaMemory", "ToolTrajectoryMemory"}),
    # Profile values live in profile instances, not graph memory nodes.
    "profile": frozenset(),
}
MEMORY_VIEW_VALUES: tuple[str, ...] = tuple(MEMORY_VIEW_TYPES)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def agent_memory_enabled() -> bool:
    """Cloud's "independent memory for agents" switch (default off)."""
    return env_flag("MEMOS_AGENT_MEMORY_ENABLED", False)


def owner_scope_enforced() -> bool:
    """Whether reads are constrained by owner scope.

    Enabled by default. Legacy nodes without owner fields are still returned
    through the ``owner_type IS NULL`` branch as long as their compatibility
    ``user_id`` matches the viewer, so enabling this before the backfill
    migration is safe.
    """
    return env_flag("MEMOS_OWNER_SCOPE_ENFORCED", True)


@dataclass(frozen=True)
class Owner:
    owner_type: Literal["user", "agent", "group", "public"]
    owner_id: str

    def as_filter(self) -> dict[str, str]:
        return {"owner_type": self.owner_type, "owner_id": self.owner_id}


@dataclass
class OwnerView:
    """One extraction / write unit.

    ``user_id`` is what the reader sees as the acting user; ``agent_id`` and
    ``group_id`` are stored as compatibility properties.
    """

    owner: Owner
    user_id: str | None
    agent_id: str | None = None
    group_id: str | None = None
    messages: list[Any] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)
    role_names: dict[str, str] = field(default_factory=dict)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def normalize_id_list(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize a ``user_id`` / ``agent_id`` field into a bounded id list."""
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list | tuple | set):
        values = [str(item) for item in value]
    else:
        raise ValueError("owner id must be a string or an array of strings")  # noqa: TRY004

    values = _dedupe([item.strip() for item in values if isinstance(item, str) and item.strip()])
    if len(values) > MAX_OWNER_IDS:
        raise ValueError(f"at most {MAX_OWNER_IDS} ids are allowed per request")
    return values


def _message_role(message: Any) -> str | None:
    if isinstance(message, dict):
        role = message.get("role")
        return str(role) if role else None
    return None


def _message_role_id(message: Any) -> str | None:
    if not isinstance(message, dict):
        return None
    role_id = message.get("role_id") or message.get("user_id")
    return str(role_id) if role_id else None


def collect_role_names(messages: list[Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role_id = _message_role_id(message)
        role_name = message.get("role_name")
        if role_id and role_name:
            names[role_id] = str(role_name)
    return names


def annotate_messages(messages: list[Any], role_names: dict[str, str]) -> list[Any]:
    """Prefix conversational messages with the speaker name.

    The reader only understands ``role`` / ``content``; carrying the speaker in
    the text is what lets extraction attribute a statement to a participant.
    Messages are copied so the request object is never mutated.
    """
    if not role_names:
        return list(messages or [])

    annotated: list[Any] = []
    for message in messages or []:
        if not isinstance(message, dict):
            annotated.append(message)
            continue
        role_id = _message_role_id(message)
        name = role_names.get(role_id or "")
        content = message.get("content")
        if not name or _message_role(message) not in ("user", "assistant") or content is None:
            annotated.append(message)
            continue
        if isinstance(content, str):
            copy = dict(message)
            copy["content"] = f"{name}: {content}"
            annotated.append(copy)
        else:
            annotated.append(message)
    return annotated


def messages_for_participant(messages: list[Any], user_id: str) -> list[Any]:
    """Keep the statements a participant owns (plus assistant context).

    Group chats used to hand the full conversation to every participant's
    extraction view, which let one participant's private view extract and store
    another participant's statements and multiplied the LLM cost by N. When the
    payload has no ``role_id`` at all (legacy single-speaker calls) the full
    conversation is kept.
    """
    has_role_ids = any(_message_role_id(message) for message in messages or [])
    if not has_role_ids:
        return list(messages or [])

    kept: list[Any] = []
    for message in messages or []:
        if not isinstance(message, dict):
            kept.append(message)
            continue
        role = _message_role(message)
        role_id = _message_role_id(message)
        if role != "user" or role_id == user_id:
            kept.append(message)
        # A user message without a role_id is ambiguous in a partially
        # annotated group conversation. Keeping it for every participant would
        # let A's private view absorb B's unlabelled statement, so drop it.
    return kept


def build_write_views(
    *,
    user_id: str | list[str] | None,
    agent_id: str | list[str] | None,
    group_id: str | None,
    messages: list[Any] | None,
    info: dict[str, Any] | None,
    allow_public: bool,
    tenant: str,
) -> list[OwnerView]:
    """Build one extraction view per owner.

    A plain single-user request keeps the historical single view (and thus the
    exact historical behavior and cost). Group chat / agent memory / public
    memory add views on top.
    """
    user_ids = normalize_id_list(user_id)
    agent_ids = normalize_id_list(agent_id)
    messages = list(messages or [])
    info = dict(info or {})
    role_names = collect_role_names(messages)

    if not user_ids and not agent_ids:
        raise ValueError("user_id or agent_id is required")

    group_id = group_id.strip() if isinstance(group_id, str) and group_id.strip() else None
    multi = len(user_ids) > 1 or bool(group_id) or allow_public or agent_memory_enabled()
    wants_agent_views = agent_memory_enabled() and bool(agent_ids)

    if not multi and not wants_agent_views:
        if not user_ids:
            # Agent-only writes exist only when independent agent memory is on.
            raise ValueError("user_id is required unless agent memory is enabled")
        # Historical path: exactly one extraction view, no message rewriting.
        return [
            OwnerView(
                owner=Owner("user", user_ids[0]),
                user_id=user_ids[0],
                agent_id=agent_ids[0] if agent_ids else None,
                group_id=group_id,
                messages=messages,
                info=info,
                role_names=role_names,
            )
        ]

    annotated = annotate_messages(messages, role_names)
    shared_agent_id = agent_ids[0] if len(agent_ids) == 1 else None
    views: list[OwnerView] = []

    for uid in user_ids:
        views.append(
            OwnerView(
                owner=Owner("user", uid),
                user_id=uid,
                agent_id=shared_agent_id,
                group_id=group_id,
                messages=messages_for_participant(annotated, uid),
                info=info,
                role_names=role_names,
            )
        )

    for aid in agent_ids:
        views.append(
            OwnerView(
                owner=Owner("agent", aid),
                user_id=None,
                agent_id=aid,
                group_id=group_id,
                messages=annotated,
                info=info,
                role_names=role_names,
            )
        )

    if group_id:
        views.append(
            OwnerView(
                owner=Owner("group", group_id),
                user_id=None,
                agent_id=shared_agent_id,
                group_id=group_id,
                messages=annotated,
                info=info,
                role_names=role_names,
            )
        )

    if allow_public:
        views.append(
            OwnerView(
                owner=Owner("public", tenant or DEFAULT_TENANT),
                user_id=None,
                agent_id=shared_agent_id,
                group_id=group_id,
                messages=annotated,
                info=info,
                role_names=role_names,
            )
        )

    return views


def stamp_owner(
    memory: Any,
    view: OwnerView,
    *,
    task_id: str | None = None,
) -> None:
    """Write owner fields (and compatibility ids) onto a memory item."""
    metadata = getattr(memory, "metadata", None)
    if metadata is None:
        return
    metadata.owner_type = view.owner.owner_type
    metadata.owner_id = view.owner.owner_id
    if view.user_id:
        metadata.user_id = view.user_id
    if view.agent_id:
        metadata.agent_id = view.agent_id
    if view.group_id:
        metadata.group_id = view.group_id
    if task_id:
        metadata.task_id = task_id


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def filter_by_allow_memory_view(memories: list[Any], allow_memory_view: list[str] | None):
    """Drop memory types the caller did not ask to generate.

    ``None`` means "no write-side trimming" (historical behavior). An empty list
    means "generate nothing", which is what the cloud contract implies.
    """
    if allow_memory_view is None:
        return memories
    allowed_types: set[str] = set()
    for view in allow_memory_view:
        allowed_types.update(MEMORY_VIEW_TYPES.get(view, frozenset()))
    return [
        memory
        for memory in memories
        if getattr(getattr(memory, "metadata", None), "memory_type", None) in allowed_types
    ]


def owner_scope_filter(
    *,
    user_ids: list[str] | None = None,
    agent_ids: list[str] | None = None,
    group_ids: list[str] | None = None,
    tenant: str | None = None,
    include_public: bool = True,
    include_legacy: bool = True,
    include_ownerless_legacy: bool = True,
) -> dict[str, Any] | None:
    """Build the ``or`` filter that limits a read to the viewer's scopes."""
    branches: list[dict[str, Any]] = []

    for uid in user_ids or []:
        branches.append({"owner_type": "user", "owner_id": uid})
        if include_legacy:
            # Nodes written before the owner migration have no owner fields.
            branches.append({"owner_type": None, "user_id": uid})
    for aid in agent_ids or []:
        branches.append({"owner_type": "agent", "owner_id": aid})
        if include_legacy:
            branches.append({"owner_type": None, "agent_id": aid})
    for gid in group_ids or []:
        branches.append({"owner_type": "group", "owner_id": gid})
    if include_legacy and include_ownerless_legacy and (user_ids or agent_ids):
        # Reorganizer / dream nodes were historically written without any
        # compatibility id. They are tenant-scoped already (``user_name``), so
        # keep them readable after owner enforcement instead of silently
        # dropping summaries.
        branches.append({"owner_type": None, "user_id": None})
    if include_public:
        public: dict[str, Any] = {"owner_type": "public"}
        if tenant:
            public["owner_id"] = tenant
        branches.append(public)

    if not branches:
        return None
    if len(branches) == 1:
        return branches[0]
    return {"or": branches}


def inject_owner_scope(
    base_filter: dict[str, Any] | None,
    scope: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """AND the viewer scope with a (possibly per-cube) client filter."""
    if not scope:
        return base_filter
    if not base_filter:
        return scope
    return {"and": [base_filter, scope]}
