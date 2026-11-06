"""Regression tests for batch dedup in the structure reorganizer.

One ``add`` request queues a *single* message carrying every memory extracted
from it, but ``handle_add`` used to call the relation detector for
``after_node[0]`` only -- the rest of the batch never reached dedup..
"""

from unittest.mock import MagicMock, patch

from memos.memories.textual.tree_text_memory.organize.reorganizer import (
    GraphStructureReorganizer,
    QueueMessage,
)


_MODULE = "memos.memories.textual.tree_text_memory.organize.reorganizer"


def _make_reorganizer():
    """Build a reorganizer without spawning its background threads."""
    with (
        patch(f"{_MODULE}.ContextThread"),
        patch(f"{_MODULE}.RelationAndReasoningDetector"),
        patch(f"{_MODULE}.NodeHandler"),
    ):
        return GraphStructureReorganizer(
            graph_store=MagicMock(),
            llm=MagicMock(),
            embedder=MagicMock(),
            is_reorganize=True,
        )


def _node(node_id, status="activated"):
    node = MagicMock()
    node.id = node_id
    node.metadata.status = status
    node.metadata.memory_type = "LongTermMemory"
    return node


def _deduped_ids(reorganizer):
    return [c.args[0].id for c in reorganizer.resolver.detect.call_args_list]


def test_handle_add_checks_every_node_in_the_batch():
    """The regression: only after_node[0] used to be considered."""
    reorganizer = _make_reorganizer()
    nodes = [_node(f"n{i}") for i in range(3)]

    reorganizer.handle_add(QueueMessage(op="add", after_node=nodes, user_name="example-agent"))

    assert _deduped_ids(reorganizer) == ["n0", "n1", "n2"]


def test_handle_add_skips_archived_nodes():
    """In-place merge keeps the newer side, so re-judging an archived node would
    promote it back to keeper and flip the preference between two rows."""
    reorganizer = _make_reorganizer()

    reorganizer.handle_add(
        QueueMessage(
            op="add",
            after_node=[_node("live"), _node("archived", status="archived")],
            user_name="example-agent",
        )
    )

    assert _deduped_ids(reorganizer) == ["live"]


def test_handle_add_does_not_reuse_a_node_merged_away_in_the_same_batch():
    """A node absorbed earlier in the batch must not judge later nodes."""
    reorganizer = _make_reorganizer()
    first, second, third = _node("a"), _node("b"), _node("c")
    reorganizer.resolver.detect.side_effect = [
        [[first, second, "redundant"]],  # "a" absorbs "b"
        [],
    ]
    reorganizer.resolver.resolve.return_value = "b"

    reorganizer.handle_add(
        QueueMessage(op="add", after_node=[first, second, third], user_name="example-agent")
    )

    assert _deduped_ids(reorganizer) == ["a", "c"]


def test_handle_add_stops_judging_a_node_that_was_itself_archived():
    """A back-filled old conversation is written later but said earlier, so resolve
    archives the new node. Its remaining candidates must not be resolved against it."""
    reorganizer = _make_reorganizer()
    new, old_1, old_2 = _node("new"), _node("old1"), _node("old2")
    reorganizer.resolver.detect.return_value = [
        [new, old_1, "supersedes"],
        [new, old_2, "supersedes"],
    ]
    reorganizer.resolver.resolve.return_value = "new"

    reorganizer.handle_add(QueueMessage(op="add", after_node=[new], user_name="example-agent"))

    assert reorganizer.resolver.resolve.call_count == 1


def test_handle_add_keeps_resolving_when_a_pair_was_a_noop():
    reorganizer = _make_reorganizer()
    new, old_1, old_2 = _node("new"), _node("old1"), _node("old2")
    reorganizer.resolver.detect.return_value = [
        [new, old_1, "supersedes"],
        [new, old_2, "supersedes"],
    ]
    reorganizer.resolver.resolve.side_effect = [None, "old2"]

    reorganizer.handle_add(QueueMessage(op="add", after_node=[new], user_name="example-agent"))

    assert reorganizer.resolver.resolve.call_count == 2


def test_handle_add_ignores_missing_entries():
    reorganizer = _make_reorganizer()

    reorganizer.handle_add(
        QueueMessage(op="add", after_node=[None, _node("n0")], user_name="example-agent")
    )

    assert _deduped_ids(reorganizer) == ["n0"]


def test_handle_add_survives_an_empty_batch():
    reorganizer = _make_reorganizer()

    reorganizer.handle_add(QueueMessage(op="add", after_node=[], user_name="example-agent"))

    reorganizer.resolver.detect.assert_not_called()


def test_handle_add_still_marks_reorganization_needed():
    reorganizer = _make_reorganizer()
    reorganizer._reorganize_needed = False

    reorganizer.handle_add(QueueMessage(op="add", after_node=[_node("n0")], user_name="example-agent"))

    assert reorganizer._reorganize_needed is True


def _converted(reorganizer, after_node):
    """Stub the id->node resolution so _preprocess_message sees the given list."""
    message = QueueMessage(op="add", after_node=["unused"], user_name="example-agent")
    reorganizer._convert_id_to_node = MagicMock(
        return_value=QueueMessage(op="add", after_node=after_node, user_name="example-agent")
    )
    return message


def test_preprocess_message_drops_only_the_missing_nodes():
    """One stale id used to discard the whole batch."""
    reorganizer = _make_reorganizer()
    survivors = [_node("a"), None, _node("c")]
    message = _converted(reorganizer, survivors)

    assert reorganizer._preprocess_message(message) is True
    assert [n.id for n in survivors if n is not None] == ["a", "c"]


def test_preprocess_message_skips_the_message_when_nothing_survives():
    reorganizer = _make_reorganizer()
    message = _converted(reorganizer, [None, None])

    assert reorganizer._preprocess_message(message) is False


def test_preprocess_message_skips_an_empty_batch():
    reorganizer = _make_reorganizer()
    message = _converted(reorganizer, [])

    assert reorganizer._preprocess_message(message) is False
