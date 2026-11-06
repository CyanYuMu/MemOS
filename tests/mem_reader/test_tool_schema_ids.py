"""ToolSchema memories get content-derived ids so repeated requests overwrite instead of piling up."""

import json

from unittest.mock import MagicMock

import pytest

from memos.mem_reader.read_multi_modal.system_parser import SystemParser
from memos.types.general_types import UserContext


def _tool(name="search", description="find things", **extra):
    schema = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }
    schema["function"].update(extra)
    return schema


def _message(*schemas):
    return {"role": "system", "content": json.dumps(list(schemas))}


@pytest.fixture
def parser():
    embedder = MagicMock()
    embedder.embed.return_value = [[0.1] * 4]
    return SystemParser(embedder=embedder)


def _ids(parser, message, user_id="u1", cube="cube-a", **kwargs):
    items = parser.parse_fine(
        message,
        {"user_id": user_id, "session_id": "s1"},
        user_context=UserContext(user_id=user_id, mem_cube_id=cube),
        **kwargs,
    )
    return [item.id for item in items]


def test_same_schema_in_two_requests_gets_the_same_id(parser):
    assert _ids(parser, _message(_tool())) == _ids(parser, _message(_tool()))


def test_key_order_inside_the_schema_does_not_change_the_id(parser):
    reordered = {"function": dict(reversed(list(_tool()["function"].items()))), "type": "function"}

    assert _ids(parser, _message(_tool())) == _ids(parser, _message(reordered))


def test_ids_are_valid_uuids(parser):
    import uuid

    for item_id in _ids(parser, _message(_tool())):
        assert str(uuid.UUID(item_id)) == item_id


def test_changed_schema_gets_a_new_id(parser):
    assert _ids(parser, _message(_tool())) != _ids(parser, _message(_tool(description="other")))


def test_same_name_with_a_different_schema_does_not_share_an_id(parser):
    assert _ids(parser, _message(_tool("a"))) != _ids(parser, _message(_tool("a", extra_flag=True)))


def test_other_tenants_and_users_do_not_share_ids(parser):
    base = _ids(parser, _message(_tool()))

    assert base != _ids(parser, _message(_tool()), cube="cube-b")
    assert base != _ids(parser, _message(_tool()), user_id="u2")


def test_tenant_falls_back_to_user_name_without_user_context(parser):
    def ids(user_name):
        items = parser.parse_fine(
            _message(_tool()), {"user_id": "u1", "session_id": "s1"}, user_name=user_name
        )
        return [item.id for item in items]

    assert ids("cube-a") == ids("cube-a")
    assert ids("cube-a") != ids("cube-b")


def test_distinct_tools_in_one_message_have_distinct_ids_and_duplicates_collapse(parser):
    ids = _ids(parser, _message(_tool("a"), _tool("b"), _tool("a")))

    assert len(ids) == 2
    assert len(set(ids)) == 2


# ---- from live messages: the <tool_schema> block of a system message ------------------------


def _live(parser, *messages, user_id="u1", cube="cube-a"):
    return parser.tool_schema_items(
        list(messages),
        {"user_id": user_id, "session_id": "s1"},
        user_context=UserContext(user_id=user_id, mem_cube_id=cube),
    )


def _wrapped(*schemas, prefix="You are a helpful agent.\n"):
    return {
        "role": "system",
        "content": f"{prefix}<tool_schema>{json.dumps(list(schemas))}</tool_schema>",
    }


def test_a_tool_schema_block_inside_a_system_prompt_becomes_memories(parser):
    items = _live(parser, _wrapped(_tool("search"), _tool("open")))

    assert [json.loads(i.memory)["function"]["name"] for i in items] == ["search", "open"]
    assert {i.metadata.memory_type for i in items} == {"ToolSchemaMemory"}


def test_a_live_block_and_a_bare_schema_message_share_ids(parser):
    live = [i.id for i in _live(parser, _wrapped(_tool()))]

    assert live == _ids(parser, _message(_tool()))


def test_the_same_block_twice_in_one_request_collapses(parser):
    items = _live(parser, _wrapped(_tool()), _wrapped(_tool(), prefix="again\n"))

    assert len(items) == 1


def test_messages_without_a_schema_block_or_not_from_the_system_are_ignored(parser):
    plain = {"role": "system", "content": "Be brief."}
    user = {"role": "user", "content": f"<tool_schema>{json.dumps([_tool()])}</tool_schema>"}
    broken = {"role": "system", "content": "<tool_schema>not a schema</tool_schema>"}

    assert (
        _live(parser, plain, user, broken, "plain string", {"role": "system", "content": None})
        == []
    )


def test_a_dict_content_with_text_is_read(parser):
    message = {"role": "system", "content": {"text": _wrapped(_tool())["content"]}}

    assert len(_live(parser, message)) == 1


def test_schemas_already_stored_are_not_embedded_again(parser):
    stored = _ids(parser, _message(_tool("search")))
    parser.embedder.embed.reset_mock()

    items = parser.parse_fine(
        _message(_tool("search"), _tool("fetch")),
        {"user_id": "u1", "session_id": "s1"},
        user_context=UserContext(user_id="u1", mem_cube_id="cube-a"),
        existing_ids=lambda ids: {i for i in ids if i in stored},
    )

    assert len(items) == 1 and items[0].id not in stored
    assert parser.embedder.embed.call_count == 1
