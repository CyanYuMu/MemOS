"""Unit tests for the time contract (``memos.time_basis``) and its API boundary."""

from unittest.mock import Mock

import pytest

from memos.memories.textual.item import TreeNodeTextualMemoryMetadata
from memos.time_basis import (
    TIME_BASIS_UTC,
    convert_legacy_node,
    is_legacy,
    normalize_metadata_times,
    normalize_payload_times,
    stamp_utc_basis,
    to_contract_time,
)


LEGACY_STAMP = "2026-10-21T10:00:00+00:00"  # 10:00 Beijing, mislabelled as UTC
LEGACY_AS_UTC = "2026-10-21T02:00:00+00:00"
REAL_UTC_STAMP = "2026-10-21T10:00:00+00:00"


class TestToContractTime:
    def test_legacy_value_is_reinterpreted_in_the_legacy_offset(self):
        assert to_contract_time(LEGACY_STAMP, legacy=True) == LEGACY_AS_UTC

    def test_utc_value_is_passed_through(self):
        assert to_contract_time(REAL_UTC_STAMP, legacy=False) == REAL_UTC_STAMP

    def test_naive_value_is_utc(self):
        assert to_contract_time("2026-10-21T10:00:00", legacy=False) == REAL_UTC_STAMP

    def test_offset_value_converts_correctly_in_both_modes(self):
        # A real offset survives both interpretations: replacing tzinfo with the
        # same offset is a no-op.
        assert to_contract_time("2026-10-21T10:00:00+08:00", legacy=False) == LEGACY_AS_UTC
        assert to_contract_time("2026-10-21T10:00:00+08:00", legacy=True) == LEGACY_AS_UTC

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty_values_are_untouched(self, value):
        assert to_contract_time(value, legacy=True) == value

    def test_unparseable_value_is_returned_unchanged(self):
        assert to_contract_time("not-a-time", legacy=True) == "not-a-time"


class TestIsLegacy:
    def test_unmarked_node_is_legacy(self):
        assert is_legacy({"memory_type": "LongTermMemory"}) is True

    def test_marked_node_is_utc(self):
        assert is_legacy({"memory_type": "LongTermMemory", "time_basis": TIME_BASIS_UTC}) is False

    @pytest.mark.parametrize("memory_type", ["DreamDiary", "InsightMemory", "Context"])
    def test_dream_nodes_are_utc_even_without_marker(self, memory_type):
        assert is_legacy({"memory_type": memory_type}) is False

    def test_projection_reads_camel_case_memory_type(self):
        assert is_legacy({"memoryType": "DreamDiary"}) is False


class TestNormalizeMetadataTimes:
    def test_legacy_fields_shift_by_the_offset(self):
        metadata = {"created_at": LEGACY_STAMP, "chat_time": LEGACY_STAMP}
        normalize_metadata_times(metadata)
        assert metadata["created_at"] == LEGACY_AS_UTC
        assert metadata["chat_time"] == LEGACY_AS_UTC

    def test_utc_fields_are_stable(self):
        metadata = {"created_at": REAL_UTC_STAMP, "time_basis": TIME_BASIS_UTC}
        normalize_metadata_times(metadata)
        assert metadata["created_at"] == REAL_UTC_STAMP

    def test_dream_fields_are_stable(self):
        metadata = {"created_at": REAL_UTC_STAMP, "memory_type": "DreamDiary"}
        normalize_metadata_times(metadata)
        assert metadata["created_at"] == REAL_UTC_STAMP


class TestNormalizePayloadTimes:
    def test_nested_history_inherits_the_node_basis(self):
        # A history entry has no marker of its own; treating it as legacy inside a
        # UTC node would shift every archived timestamp by eight hours.
        payload = {
            "data": [
                {
                    "id": "m1",
                    "metadata": {
                        "time_basis": TIME_BASIS_UTC,
                        "created_at": REAL_UTC_STAMP,
                        "history": [{"created_at": REAL_UTC_STAMP}],
                    },
                }
            ]
        }
        normalize_payload_times(payload)
        metadata = payload["data"][0]["metadata"]
        assert metadata["created_at"] == REAL_UTC_STAMP
        assert metadata["history"][0]["created_at"] == REAL_UTC_STAMP

    def test_legacy_node_and_its_history_both_shift(self):
        payload = {
            "code": 200,
            "data": {
                "metadata": {"created_at": LEGACY_STAMP, "history": [{"created_at": LEGACY_STAMP}]}
            },
        }
        normalize_payload_times(payload)
        metadata = payload["data"]["metadata"]
        assert metadata["created_at"] == LEGACY_AS_UTC
        assert metadata["history"][0]["created_at"] == LEGACY_AS_UTC

    def test_sources_and_embeddings_are_not_walked(self):
        payload = {
            "metadata": {
                "time_basis": TIME_BASIS_UTC,
                "sources": [{"chat_time": LEGACY_STAMP}],
                "embedding": [0.1, 0.2],
            }
        }
        normalize_payload_times(payload)
        assert payload["metadata"]["sources"][0]["chat_time"] == LEGACY_STAMP


class TestConvertLegacyNode:
    def test_converts_and_marks(self):
        metadata = {"created_at": LEGACY_STAMP, "memory_type": "LongTermMemory"}
        assert convert_legacy_node(metadata) is True
        assert metadata["created_at"] == LEGACY_AS_UTC
        assert metadata["time_basis"] == TIME_BASIS_UTC

    def test_utc_node_is_left_alone(self):
        metadata = {"created_at": REAL_UTC_STAMP, "time_basis": TIME_BASIS_UTC}
        assert convert_legacy_node(metadata) is False
        assert metadata["time_basis"] == TIME_BASIS_UTC

    def test_skipped_fields_are_not_converted(self):
        metadata = {"created_at": LEGACY_STAMP, "updated_at": LEGACY_STAMP}
        convert_legacy_node(metadata, skip={"updated_at"})
        assert metadata["created_at"] == LEGACY_AS_UTC
        assert metadata["updated_at"] == LEGACY_STAMP


class TestStampUtcBasis:
    def test_overrides_an_explicit_none(self):
        # ``model_dump()`` without ``exclude_none`` writes ``time_basis=None``; a
        # plain setdefault would leave the node unmarked and therefore "legacy".
        assert stamp_utc_basis({"time_basis": None})["time_basis"] == TIME_BASIS_UTC


class TestGraphWritePath:
    def test_prepare_node_metadata_stamps_utc(self):
        from memos.graph_dbs.neo4j import _prepare_node_metadata

        metadata = _prepare_node_metadata({"memory_type": "LongTermMemory"})
        assert metadata["time_basis"] == TIME_BASIS_UTC
        assert metadata["created_at"].endswith("+00:00")

    def test_import_path_keeps_the_source_basis(self):
        from memos.graph_dbs.neo4j import _prepare_node_metadata

        metadata = _prepare_node_metadata(
            {"created_at": LEGACY_STAMP, "memory_type": "LongTermMemory"},
            stamp_basis=False,
        )
        assert "time_basis" not in metadata


class TestMergeUsesPerSideBasis:
    def _handler(self):
        from memos.memories.textual.tree_text_memory.organize.handler import NodeHandler

        embedder = Mock()
        embedder.embed.return_value = [[0.1, 0.2]]
        return NodeHandler(graph_store=Mock(), llm=Mock(), embedder=embedder)

    def _metadata(self, **overrides):
        base = {
            "memory_type": "LongTermMemory",
            "sources": [],
            "created_at": LEGACY_STAMP,
            "chat_time": None,
        }
        base.update(overrides)
        return TreeNodeTextualMemoryMetadata.model_validate(base)

    def test_earliest_created_at_is_chosen_in_real_time(self):
        handler = self._handler()
        # 10:00 Beijing (=02:00 UTC) is earlier than 03:00 UTC, although the raw
        # strings would sort the other way round.
        legacy = self._metadata(created_at=LEGACY_STAMP)
        utc = self._metadata(created_at="2026-10-21T03:00:00+00:00", time_basis=TIME_BASIS_UTC)
        merged = handler._merge_metadata("both true", legacy, utc)
        assert merged.created_at == LEGACY_AS_UTC
        assert merged.time_basis == TIME_BASIS_UTC
