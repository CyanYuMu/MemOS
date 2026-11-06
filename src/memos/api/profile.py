"""Profile (attribute) memory: templates, bound instances, extraction and recall.

The template is a JSON tree of at most three key levels; every leaf is
``{"value": ..., "algorithm_updatable": bool}``. A template is bound to a
subject (user or agent) and produces one instance holding the field values.

Cascade rules:

* template update adds missing fields with ``None`` and marks removed fields
  ``deprecated`` (values are retained);
* template delete marks bound instances ``detached`` instead of destroying
  user data; explicit instance delete is a separate operation;
* extraction never overwrites a field whose ``algorithm_updatable`` is false.
"""

from __future__ import annotations

import json
import os
import re
import uuid

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from memos.log import get_logger
from memos.mem_reader.custom_extract import apply_custom_extract_prompt


if TYPE_CHECKING:
    from memos.api.cloud_store import MetadataStore


logger = get_logger(__name__)

PROFILE_TEMPLATE_KIND = "profile_template"
PROFILE_INSTANCE_KIND = "profile_instance"
PROFILE_MEMORY_TYPE = "ProfileMemory"
MAX_TEMPLATE_DEPTH = 3


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class ProfileValidationError(ValueError):
    """Raised when a template tree / field edit is invalid."""


@dataclass
class ProfileService:
    store: MetadataStore

    # ── templates ────────────────────────────────────────────────────────────

    def add_template(self, *, user_name: str, name: str, metadata: dict[str, Any]) -> dict:
        name = (name or "").strip()
        if not name:
            raise ProfileValidationError("profile template name is required")
        template_tree = normalize_template(metadata)
        now = _utc_now()
        document = {
            "entity_id": _new_id("tpl"),
            "user_name": user_name,
            "name": name,
            "metadata": template_tree,
            "deprecated_paths": [],
            "created_at": now,
            "updated_at": now,
        }
        self.store.put(PROFILE_TEMPLATE_KIND, document)
        return document

    def list_templates(
        self, *, user_name: str, page: int | None = None, page_size: int | None = None
    ) -> tuple[list[dict], int]:
        documents = self.store.list(PROFILE_TEMPLATE_KIND, user_name=user_name)
        total = len(documents)
        if page_size:
            start = (max(page or 1, 1) - 1) * page_size
            documents = documents[start : start + page_size]
        return [self._template_view(doc, user_name) for doc in documents], total

    def get_template(self, *, user_name: str, template_id: str) -> dict | None:
        document = self._owned_template(user_name, template_id)
        return self._template_view(document, user_name) if document else None

    def update_template(
        self,
        *,
        user_name: str,
        template_id: str,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict | None:
        document = self._owned_template(user_name, template_id)
        if document is None:
            return None

        if name is not None:
            document["name"] = name.strip() or document["name"]

        affected = 0
        if metadata is not None:
            new_tree = normalize_template(metadata)
            old_leaves = flatten_template(document.get("metadata") or {})
            new_leaves = flatten_template(new_tree)

            merged_tree = _merge_tree(document.get("metadata") or {}, new_tree)
            deprecated = set(document.get("deprecated_paths") or [])
            for path in old_leaves:
                if path not in new_leaves:
                    deprecated.add(path)
            for path in new_leaves:
                deprecated.discard(path)

            changed = {path for path, leaf in new_leaves.items() if old_leaves.get(path) != leaf}
            document["metadata"] = merged_tree
            document["deprecated_paths"] = sorted(deprecated)

            affected = self._cascade_template_update(
                user_name=user_name,
                template_id=template_id,
                template_name=document["name"],
                template_tree=merged_tree,
                deprecated_paths=document["deprecated_paths"],
                changed_paths=changed,
            )

        document["updated_at"] = _utc_now()
        self.store.put(PROFILE_TEMPLATE_KIND, document)
        return {**self._template_view(document, user_name), "affected_instances": affected}

    def delete_template(self, *, user_name: str, template_id: str) -> bool:
        document = self._owned_template(user_name, template_id)
        if document is None:
            return False
        for instance in self._instances(user_name=user_name, template_id=template_id):
            instance["status"] = "detached"
            instance["updated_at"] = _utc_now()
            self.store.put(PROFILE_INSTANCE_KIND, instance)
        return self.store.delete(PROFILE_TEMPLATE_KIND, template_id)

    # ── instances ────────────────────────────────────────────────────────────

    def bind(self, *, user_name: str, bind_list: list[dict[str, Any]]) -> list[dict]:
        bound: list[dict] = []
        for item in bind_list or []:
            template_id = str(item.get("profile_template_id") or "").strip()
            subject_type, subject_id = _subject(item)
            if not template_id:
                raise ProfileValidationError("profile_template_id is required")
            template = self._owned_template(user_name, template_id)
            if template is None:
                raise ProfileValidationError(f"profile template not found: {template_id}")

            instance = self._find_instance(user_name, template_id, subject_type, subject_id)
            if instance is None:
                now = _utc_now()
                instance = {
                    "entity_id": _new_id("profile"),
                    "user_name": user_name,
                    "template_id": template_id,
                    "template_name": template["name"],
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "status": "bound",
                    "fields": {},
                    "created_at": now,
                    "updated_at": now,
                }
            instance["status"] = "bound"
            instance["template_name"] = template["name"]
            _sync_instance_fields(
                instance,
                template.get("metadata") or {},
                template.get("deprecated_paths") or [],
                reset_missing=False,
            )
            instance["updated_at"] = _utc_now()
            self.store.put(PROFILE_INSTANCE_KIND, instance)
            bound.append(self.instance_view(instance))
        return bound

    def edit(
        self,
        *,
        user_name: str,
        template_id: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        remove_fields: list[str] | None = None,
    ) -> dict | None:
        subject_type, subject_id = _subject({"user_id": user_id, "agent_id": agent_id})
        instance = self._find_instance(user_name, template_id, subject_type, subject_id)
        if instance is None:
            return None
        template = self._owned_template(user_name, template_id)
        allowed = flatten_template((template or {}).get("metadata") or {})
        fields = instance.setdefault("fields", {})

        for path, change in flatten_edits(metadata or {}).items():
            current = fields.get(path) or {}
            kept_flag = current.get(
                "algorithm_updatable", allowed.get(path, {}).get("algorithm_updatable", True)
            )
            fields[path] = {
                **current,
                "value": change["value"] if "value" in change else current.get("value"),
                "algorithm_updatable": bool(change.get("algorithm_updatable", kept_flag)),
                "deprecated": False,
                **({} if path in allowed else {"custom": True}),
            }

        for path in remove_fields or []:
            if path in fields:
                fields[path]["value"] = None
                fields[path]["deprecated"] = True

        instance["updated_at"] = _utc_now()
        self.store.put(PROFILE_INSTANCE_KIND, instance)
        return self.instance_view(instance)

    def delete_instance(
        self,
        *,
        user_name: str,
        template_id: str,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> bool:
        subject_type, subject_id = _subject({"user_id": user_id, "agent_id": agent_id})
        instance = self._find_instance(user_name, template_id, subject_type, subject_id)
        if instance is None:
            return False
        return self.store.delete(PROFILE_INSTANCE_KIND, instance["entity_id"])

    def query(
        self,
        *,
        user_name: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        template_id: str | None = None,
    ) -> list[dict]:
        subject_type, subject_id = _subject({"user_id": user_id, "agent_id": agent_id})
        instances = self._instances(
            user_name=user_name,
            template_id=template_id,
            subject_type=subject_type,
            subject_id=subject_id,
        )
        return [self.instance_view(instance) for instance in instances]

    # ── extraction / recall ──────────────────────────────────────────────────

    def extract_and_update(
        self,
        *,
        user_name: str,
        llm: Any,
        conversation: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        template_id: str | None = None,
    ) -> list[dict]:
        """Map conversation facts onto bound template fields.

        Only fields with ``algorithm_updatable=true`` are written, and existing
        values are never cleared by an empty extraction result.
        """
        if llm is None or not conversation:
            return []

        subject_type, subject_id = _subject({"user_id": user_id, "agent_id": agent_id})
        instances = self._instances(
            user_name=user_name,
            template_id=template_id,
            subject_type=subject_type,
            subject_id=subject_id,
            status="bound",
        )
        updated: list[dict] = []
        for instance in instances:
            template = self._owned_template(user_name, instance["template_id"]) or {}
            updatable = {
                path: leaf
                for path, leaf in flatten_template(template.get("metadata") or {}).items()
                if leaf.get("algorithm_updatable", True)
            }
            if not updatable:
                continue

            current = {
                path: (instance.get("fields", {}).get(path) or {}).get("value")
                for path in updatable
            }
            values = _extract_values(llm, conversation, updatable, current)
            if not values:
                continue

            fields = instance.setdefault("fields", {})
            changed = False
            for path, value in values.items():
                if path not in updatable or value in (None, "", [], {}):
                    continue
                if fields.get(path, {}).get("value") == value:
                    continue
                fields[path] = {
                    "value": value,
                    "algorithm_updatable": True,
                    "deprecated": False,
                }
                changed = True

            if changed:
                instance["updated_at"] = _utc_now()
                self.store.put(PROFILE_INSTANCE_KIND, instance)
                updated.append(self.instance_view(instance))
        return updated

    def recall(
        self,
        *,
        user_name: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        template_id: str | None = None,
        relativity: float | None = None,
        top_k: int | None = None,
    ) -> list[dict]:
        """Return instance fields shaped like cloud's ``profile_detail_list``."""
        subject_type, subject_id = _subject({"user_id": user_id, "agent_id": agent_id})
        # Work on the stored documents: ``query`` returns public views that drop
        # the raw ``fields`` map this method needs.
        instances = self._instances(
            user_name=user_name,
            template_id=template_id,
            subject_type=subject_type,
            subject_id=subject_id,
        )
        memories: list[dict] = []
        for instance in instances:
            if instance.get("status") != "bound":
                continue
            template_id_value = instance.get("template_id")
            for path, field in (instance.get("fields") or {}).items():
                value = field.get("value")
                if value in (None, "", [], {}) or field.get("deprecated"):
                    continue
                category, _, name = path.rpartition(".")
                memories.append(
                    {
                        "id": f"{instance['entity_id']}_{path}",
                        "memory": f"{path}: {value}",
                        "memory_type": PROFILE_MEMORY_TYPE,
                        "template_id": template_id_value,
                        "template_name": instance.get("template_name"),
                        "profile_category": category or path,
                        "profile_field": name or path,
                        "profile_path": path,
                        "status": "activated",
                        "confidence": 1.0,
                        "relativity": relativity,
                        "algorithm_updatable": field.get("algorithm_updatable", True),
                        "metadata": {
                            "memory_type": PROFILE_MEMORY_TYPE,
                            "status": "activated",
                            "key": path,
                            "template_id": template_id_value,
                            "profile_path": path,
                        },
                    }
                )
        if top_k is not None and top_k >= 0:
            memories = memories[:top_k]
        return memories

    # ── internals ────────────────────────────────────────────────────────────

    def _owned_template(self, user_name: str, template_id: str) -> dict | None:
        document = self.store.get(PROFILE_TEMPLATE_KIND, template_id)
        if document is None or document.get("user_name") != user_name:
            return None
        return document

    def _instances(
        self,
        *,
        user_name: str,
        template_id: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        documents = self.store.list(PROFILE_INSTANCE_KIND, user_name=user_name)
        return [
            document
            for document in documents
            if (template_id is None or document.get("template_id") == template_id)
            and (subject_type is None or document.get("subject_type") == subject_type)
            and (subject_id is None or document.get("subject_id") == subject_id)
            and (status is None or document.get("status") == status)
        ]

    def _find_instance(
        self, user_name: str, template_id: str, subject_type: str, subject_id: str
    ) -> dict | None:
        matches = self._instances(
            user_name=user_name,
            template_id=template_id,
            subject_type=subject_type,
            subject_id=subject_id,
        )
        return matches[0] if matches else None

    def _cascade_template_update(
        self,
        *,
        user_name: str,
        template_id: str,
        template_name: str,
        template_tree: dict[str, Any],
        deprecated_paths: list[str],
        changed_paths: set[str],
    ) -> int:
        instances = self._instances(user_name=user_name, template_id=template_id)
        for instance in instances:
            instance["template_name"] = template_name
            _sync_instance_fields(
                instance,
                template_tree,
                deprecated_paths,
                reset_missing=False,
                reset_flags_of=changed_paths,
            )
            instance["updated_at"] = _utc_now()
            self.store.put(PROFILE_INSTANCE_KIND, instance)
        return len(instances)

    def _template_view(self, document: dict, user_name: str) -> dict:
        bound_count = len(
            self._instances(user_name=user_name, template_id=document["entity_id"], status="bound")
        )
        return {
            "id": document["entity_id"],
            "profile_template_id": document["entity_id"],
            "name": document.get("name"),
            "metadata": document.get("metadata") or {},
            "deprecated_paths": document.get("deprecated_paths") or [],
            "bound_count": bound_count,
            "instance_count": bound_count,
            "created_at": document.get("created_at"),
            "updated_at": document.get("updated_at"),
            "create_time": document.get("created_at"),
            "update_time": document.get("updated_at"),
        }

    @staticmethod
    def instance_view(instance: dict) -> dict:
        return {
            "id": instance["entity_id"],
            "profile_template_id": instance.get("template_id"),
            "template_name": instance.get("template_name"),
            "subject_type": instance.get("subject_type"),
            "subject_id": instance.get("subject_id"),
            "status": instance.get("status"),
            "metadata": {
                path: {
                    "value": field.get("value"),
                    "algorithm_updatable": field.get("algorithm_updatable", True),
                    **({"deprecated": True} if field.get("deprecated") else {}),
                }
                for path, field in (instance.get("fields") or {}).items()
            },
            "created_at": instance.get("created_at"),
            "updated_at": instance.get("updated_at"),
        }


# ── template helpers ─────────────────────────────────────────────────────────


def normalize_template(metadata: Any) -> dict[str, Any]:
    """Validate and copy a template tree (max 3 key levels)."""
    if not isinstance(metadata, dict) or not metadata:
        raise ProfileValidationError("template metadata must be a non-empty object")
    return _normalize_node(metadata, depth=1)


def _normalize_node(node: dict[str, Any], depth: int) -> dict[str, Any]:
    if depth > MAX_TEMPLATE_DEPTH:
        raise ProfileValidationError(f"template nesting exceeds {MAX_TEMPLATE_DEPTH} levels")

    normalized: dict[str, Any] = {}
    for key, value in node.items():
        if not isinstance(key, str) or not key.strip():
            raise ProfileValidationError("template field names must be non-empty strings")
        if not isinstance(value, dict):
            raise ProfileValidationError(f"template field {key!r} must be an object")

        if _is_leaf(value):
            normalized[key] = {
                "value": _copy_value(value.get("value")),
                "algorithm_updatable": bool(value.get("algorithm_updatable", True)),
            }
        else:
            normalized[key] = _normalize_node(value, depth + 1)
    return normalized


def _is_leaf(value: dict[str, Any]) -> bool:
    return "value" in value or "algorithm_updatable" in value


def _copy_value(value: Any) -> Any:
    if isinstance(value, dict | list):
        return json.loads(json.dumps(value, default=str))
    return value


def flatten_template(tree: dict[str, Any], prefix: str = "") -> dict[str, dict[str, Any]]:
    """``{path: {value, algorithm_updatable}}`` for every leaf."""
    leaves: dict[str, dict[str, Any]] = {}
    for key, value in (tree or {}).items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and _is_leaf(value):
            leaves[path] = {
                "value": value.get("value"),
                "algorithm_updatable": bool(value.get("algorithm_updatable", True)),
            }
        elif isinstance(value, dict):
            leaves.update(flatten_template(value, path))
    return leaves


def flatten_edits(metadata: dict[str, Any], prefix: str = "") -> dict[str, dict[str, Any]]:
    """Accept a nested tree or a ``{path: value}`` map; a leaf carries only the keys it was given."""
    edits: dict[str, dict[str, Any]] = {}
    for key, value in (metadata or {}).items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and _is_leaf(value):
            edits[path] = {k: value[k] for k in ("value", "algorithm_updatable") if k in value}
        elif isinstance(value, dict):
            edits.update(flatten_edits(value, path))
        else:
            edits[path] = {"value": value}
    return edits


def _merge_tree(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Merge ``new`` into ``old``, keeping removed branches (marked deprecated)."""
    merged = json.loads(json.dumps(old, default=str))
    old_leaves = flatten_template(old)
    new_leaves = flatten_template(new)
    for path in old_leaves:
        if path not in new_leaves:
            _mark_path_deprecated(merged, path)
    for path, leaf in new_leaves.items():
        _set_path(
            merged,
            path,
            {
                "value": _copy_value(leaf["value"]),
                "algorithm_updatable": bool(leaf.get("algorithm_updatable", True)),
            },
        )
    return merged


def _mark_path_deprecated(tree: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    node: Any = tree
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return
    if isinstance(node, dict) and isinstance(node.get(parts[-1]), dict):
        node[parts[-1]]["deprecated"] = True


def _set_path(tree: dict[str, Any], path: str, leaf: dict[str, Any]) -> None:
    parts = path.split(".")
    node = tree
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict) or _is_leaf(child):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = leaf


def _sync_instance_fields(
    instance: dict,
    template_tree: dict[str, Any],
    deprecated_paths: list[str],
    *,
    reset_missing: bool,
    reset_flags_of: set[str] | None = None,
) -> None:
    fields = instance.setdefault("fields", {})
    leaves = flatten_template(template_tree)
    deprecated = set(deprecated_paths or [])

    for path, leaf in leaves.items():
        current = fields.get(path)
        if current is None:
            fields[path] = {
                "value": _copy_value(leaf.get("value")),
                "algorithm_updatable": bool(leaf.get("algorithm_updatable", True)),
                "deprecated": path in deprecated,
            }
            continue
        if reset_flags_of is None or path in reset_flags_of:
            current["algorithm_updatable"] = bool(leaf.get("algorithm_updatable", True))
        current.setdefault("value", _copy_value(leaf.get("value")))
        current["deprecated"] = path in deprecated

    for path in list(fields):
        if path not in leaves and not fields[path].get("custom"):
            fields[path]["deprecated"] = True
            if reset_missing:
                fields[path]["value"] = None


def _subject(item: dict[str, Any]) -> tuple[str, str]:
    user_id = item.get("user_id")
    agent_id = item.get("agent_id")
    if bool(user_id) == bool(agent_id):
        raise ProfileValidationError("exactly one of user_id or agent_id is required")
    if user_id:
        return "user", str(user_id)
    return "agent", str(agent_id)


_EXTRACT_PROMPT = """You maintain a structured user profile. Read the conversation and
return ONLY a JSON object mapping profile field paths to extracted values.

Allowed field paths (do not invent new ones):
{fields}

Rules:
- Output `{{}}` when nothing relevant is mentioned.
- A value must be a short string; use `null` when the conversation says nothing about a field.
- Do not output paths that are not listed above.
- Do not wrap the JSON in markdown.

Current values:
{current}

Conversation:
{conversation}
"""


def _extract_values(
    llm: Any,
    conversation: str,
    updatable: dict[str, dict[str, Any]],
    current: dict[str, Any],
) -> dict[str, Any]:
    prompt = _EXTRACT_PROMPT.format(
        fields="\n".join(f"- {path}" for path in updatable),
        current=json.dumps(current, ensure_ascii=False, default=str),
        conversation=conversation[:12000],
    )
    prompt = apply_custom_extract_prompt(prompt, "profile")
    try:
        result = llm.generate([{"role": "user", "content": prompt}])
    except Exception as e:
        logger.warning("Profile extraction LLM call failed: %s", e, exc_info=True)
        return {}

    payload = _parse_json_object(result)
    if not payload:
        return {}
    return {
        str(path): _normalize_extracted(value)
        for path, value in payload.items()
        if isinstance(path, str)
    }


def _parse_json_object(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_extracted(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return value


def profile_reserved_top_k() -> int:
    raw = os.getenv("PROFILE_MEMORY_RESERVED_TOP_K", "3")
    try:
        return max(0, int(raw))
    except ValueError:
        return 3
