"""Fixed, public calibration of the complete isolated Feedback implementation.

Run once in a fresh probe child: public hook registration intentionally lasts
until that child exits. This is an environment baseline, not Skill evidence.
"""

from __future__ import annotations

import copy
import json
import time

from types import SimpleNamespace
from unittest.mock import create_autospec


NODE_ID = "11111111-1111-4111-8111-111111111111"
BINDING_ID = "22222222-2222-4222-8222-222222222222"
USER = "public-calibration-user"
OLD_TEXT = "Calibration address A"
NEW_TEXT = "Calibration address B"
EMBEDDING = [0.9, 0.1]


class _GraphBoundary:
    """Observe only public boundary calls; never create a real storage client."""

    def __init__(self, *, separate: bool, failing: bool, events: list[str]):
        from memos.graph_dbs.base import BaseGraphDB

        self.events = events
        self.failing = failing
        self.vec_db = object() if separate else None
        self.fields = None
        self.vector_payload = None
        self.attempts = 0
        bound_contract = BaseGraphDB.update_node.__get__(self, type(self))
        self.update_node = create_autospec(bound_contract, side_effect=self._write)

    def _write(self, id, fields, user_name=None):
        assert id == NODE_ID and user_name == USER
        assert type(fields) is dict
        self.events.append("graph_write")
        self.attempts += 1
        if self.failing:
            raise RuntimeError("public calibration graph failure")
        self.fields = copy.deepcopy(fields)

    def _sync_vector_payload(self, id, fields):
        assert id == NODE_ID and self.fields is not None
        self.events.append("vector_sync")
        self.vector_payload = copy.deepcopy(fields)

    def delete_node(self, id, user_name=None):
        assert id == BINDING_ID and user_name == USER and self.fields is not None
        self.events.append("binding_cleanup")

    def add_node(self, **kwargs):
        raise AssertionError("the in-place baseline must not create an archive node")


def _inputs():
    from memos.memories.textual.item import TextualMemoryItem, TreeNodeTextualMemoryMetadata

    old = TextualMemoryItem(
        id=NODE_ID,
        memory=OLD_TEXT,
        metadata=TreeNodeTextualMemoryMetadata(
            memory_type="LongTermMemory",
            status="activated",
            key="address",
            tags=["calibration"],
            embedding=[0.1, 0.9],
            background=f"[working_binding:{BINDING_ID}]",
            file_ids=["public-calibration-document"],
            created_at="2026-10-01T10:00:00+00:00",
            time_basis="utc",
        ),
    )
    new = TextualMemoryItem(
        id="33333333-3333-4333-8333-333333333333",
        memory=NEW_TEXT,
        metadata=TreeNodeTextualMemoryMetadata(
            memory_type="LongTermMemory",
            status="activated",
            key="address",
            tags=["calibration", "corrected"],
            embedding=EMBEDDING,
            background="public calibration correction",
            created_at="2026-11-01T10:00:00+00:00",
            chat_time="2026-11-01T10:00:00+00:00",
            time_basis="utc",
        ),
    )
    return old, new


def _case(*, separate: bool = False, failing: bool = False, events=None, hook=False):
    from tenacity import RetryError

    from memos.mem_feedback.feedback import MemFeedback

    events = [] if events is None else events
    graph = _GraphBoundary(separate=separate, failing=failing, events=events)
    feedback = MemFeedback.__new__(MemFeedback)
    feedback.graph_store = graph
    feedback.mem_reader = SimpleNamespace(memory_version_switch="on")
    old, new = _inputs()
    before_old, before_new = old.model_dump(), new.model_dump()
    started = time.monotonic()
    result, error = None, None
    try:
        result = feedback._single_update_operation(old, new, "public-calibration-id", USER)
    except RetryError as exc:
        error = exc
    assert old.model_dump() == before_old and new.model_dump() == before_new
    report = {
        "passed": True,
        "graphWriteAttempts": graph.attempts,
        "events": events,
        "oldInputUnchanged": True,
        "newInputUnchanged": True,
        "elapsedSeconds": time.monotonic() - started,
    }
    if failing:
        assert error is not None and type(error.last_attempt.exception()) is RuntimeError
        assert isinstance(error.__cause__, RuntimeError)
        assert graph.attempts == 3 and graph.update_node.call_count == 3
        assert events == ["graph_write"] * 3
        assert graph.fields is None and graph.vector_payload is None and result is None
        return {
            **report,
            "exceptionType": type(error).__name__,
            "causeType": type(error.__cause__).__name__,
        }
    assert error is None
    assert result == {
        "id": NODE_ID,
        "text": NEW_TEXT,
        "source_doc_id": "public-calibration-document",
        "archived_id": NODE_ID,
        "origin_memory": OLD_TEXT,
    }
    assert graph.attempts == 1 and graph.update_node.call_count == 1
    fields = graph.fields
    assert fields["memory"] == NEW_TEXT and fields["version"] == 2
    assert fields["key"] == ("registered-hook-key" if hook else "address")
    assert fields["tags"] == ["calibration", "corrected"]
    history = [json.loads(entry) for entry in fields["history"]]
    assert len(history) == 1 and history[0]["memory"] == OLD_TEXT
    assert history[0]["update_type"] == "feedback" and history[0]["version"] == 1
    assert history[0]["timespec"]["valid_to"] == fields["valid_from"]
    expected_events = (["hook"] if hook else []) + ["graph_write"]
    if separate:
        assert "embedding" not in fields
        assert graph.vector_payload == {"embedding": EMBEDDING, "memory": NEW_TEXT}
        expected_events.append("vector_sync")
    else:
        assert fields["embedding"] == EMBEDDING and graph.vector_payload is None
    assert events == [*expected_events, "binding_cleanup"]
    return {
        **report,
        "fields": fields,
        "history": history,
        "result": result,
        "vectorPayload": graph.vector_payload,
    }


def run_health_checks() -> dict:
    """Run the four fixed checks with original Tenacity retries and waiting."""
    from memos.memories.textual.tree_text_memory.organize import versioning
    from memos.plugins.hook_defs import H
    from memos.plugins.hooks import has_hook, register_hook

    hook_name = H.MEMORY_VERSION_APPLY_FEEDBACK_UPDATE
    assert not has_hook(hook_name), "the calibration requires a fresh hook registry"
    cases = {
        "ordinary_fallback": _case(),
        "separate_vector_fallback": _case(separate=True),
        "persistent_graph_failure": _case(separate=True, failing=True),
    }
    events = []

    def public_hook(*, old_item, new_item, user_name):
        assert user_name == USER
        events.append("hook")
        updated, archive, metadata, fields = versioning.apply_feedback_update(
            old_item, new_item, user_name
        )
        fields["key"] = "registered-hook-key"
        return updated, archive, metadata, fields

    register_hook(hook_name, public_hook)
    cases["registered_hook"] = _case(events=events, hook=True)
    return {"passed": True, "cases": cases}
