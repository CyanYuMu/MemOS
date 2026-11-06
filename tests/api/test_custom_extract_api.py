"""``custom_extract_prompt`` request plumbing: validation, sync scope, async hand-off."""

import json

from types import SimpleNamespace

import pytest

from pydantic import ValidationError

from memos.api.handlers.add_handler import AddHandler
from memos.api.product_models import APIADDRequest, APIExtractRequest
from memos.mem_reader.custom_extract import current_custom_extract_prompt
from memos.mem_scheduler.schemas.task_schemas import MEM_READ_TASK_LABEL
from memos.mem_scheduler.task_schedule_modules.handlers.mem_read_handler import (
    MemReadMessageHandler,
)
from memos.multi_mem_cube.single_cube import SingleCubeView
from memos.types import UserContext


MESSAGES = [{"role": "user", "content": "I booked a flight to Tokyo"}]


@pytest.mark.parametrize("model", [APIADDRequest, APIExtractRequest])
def test_request_normalizes_the_override(model):
    req = model(
        user_id="u",
        writable_cube_ids=["c"],
        messages=MESSAGES,
        custom_extract_prompt={"preference": "  only travel  "},
    )

    assert req.custom_extract_prompt == {"preference": "only travel"}


@pytest.mark.parametrize("model", [APIADDRequest, APIExtractRequest])
def test_request_defaults_to_no_override(model):
    req = model(user_id="u", writable_cube_ids=["c"], messages=MESSAGES)

    assert req.custom_extract_prompt is None


@pytest.mark.parametrize("model", [APIADDRequest, APIExtractRequest])
@pytest.mark.parametrize(
    "bad", [{"nope": "x"}, {"image": ""}, {"image": 1}, "text", {"image": "x" * 4001}]
)
def test_request_rejects_a_malformed_override(model, bad):
    with pytest.raises(ValidationError):
        model(user_id="u", writable_cube_ids=["c"], messages=MESSAGES, custom_extract_prompt=bad)


# ---- sync path -------------------------------------------------------------------------


class _SpyCube:
    def __init__(self):
        self.seen_on_add = "unset"
        self.seen_on_extract = "unset"

    def add_memories(self, req):
        self.seen_on_add = current_custom_extract_prompt()
        return []

    def extract_memories(self, req):
        self.seen_on_extract = current_custom_extract_prompt()
        return []


def _handler(cube):
    handler = object.__new__(AddHandler)
    handler.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    handler._build_cube_view = lambda req: cube
    return handler


def test_add_scopes_the_override_to_the_request():
    cube = _SpyCube()
    req = APIADDRequest(
        user_id="u",
        writable_cube_ids=["c"],
        messages=MESSAGES,
        custom_extract_prompt={"event": "only trips"},
    )

    _handler(cube).handle_add_memories(req)

    assert cube.seen_on_add == {"event": "only trips"}
    assert current_custom_extract_prompt() is None


def test_add_without_override_sees_none():
    cube = _SpyCube()
    req = APIADDRequest(user_id="u", writable_cube_ids=["c"], messages=MESSAGES)

    _handler(cube).handle_add_memories(req)

    assert cube.seen_on_add is None


def test_extract_scopes_the_override_to_the_request():
    cube = _SpyCube()
    req = APIExtractRequest(
        user_id="u",
        writable_cube_ids=["c"],
        messages=MESSAGES,
        custom_extract_prompt={"image": "visible text only"},
    )

    _handler(cube).handle_extract_memories(req)

    assert cube.seen_on_extract == {"image": "visible text only"}
    assert current_custom_extract_prompt() is None


# ---- async path ------------------------------------------------------------------------


def test_async_message_carries_the_override():
    submitted = []
    cube = object.__new__(SingleCubeView)
    cube.logger = SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None)
    cube.cube_id = "c"
    cube.naive_mem_cube = SimpleNamespace()
    cube.mem_scheduler = SimpleNamespace(
        submit_messages=lambda messages: submitted.extend(messages)
    )
    req = APIADDRequest(
        user_id="u",
        writable_cube_ids=["c"],
        messages=MESSAGES,
        info={"app": "x"},
        custom_extract_prompt={"preference": "only travel"},
    )

    cube._schedule_memory_tasks(req, UserContext(), ["m1"], "async")

    message = submitted[0]
    assert message.label == MEM_READ_TASK_LABEL
    assert message.info["custom_extract_prompt"] == {"preference": "only travel"}
    assert message.info["app"] == "x"


class _Reader:
    save_rawfile = False
    graph_db = None

    def __init__(self):
        self.seen = "unset"
        self.kwargs = None

    def fine_transfer_simple_mem(self, items, **kwargs):
        self.seen = current_custom_extract_prompt()
        self.kwargs = kwargs
        return []


def _scheduler_handler(reader):
    handler = object.__new__(MemReadMessageHandler)
    handler.scheduler_context = SimpleNamespace(get_mem_reader=lambda: reader)
    return handler


def _process(handler, info):
    text_mem = SimpleNamespace(get=lambda mem_id, user_name=None: SimpleNamespace(metadata=None))
    handler._process_memories_with_reader(
        mem_ids=["m1"],
        user_id="u",
        mem_cube_id="c",
        text_mem=text_mem,
        user_name="c",
        task_id="t",
        info=info,
    )


def test_scheduler_applies_the_override_around_the_reader():
    reader = _Reader()

    _process(
        _scheduler_handler(reader),
        {"custom_extract_prompt": {"chat": "only deploy"}},
    )

    assert reader.seen == {"chat": "only deploy"}
    assert current_custom_extract_prompt() is None


def test_scheduler_without_override_runs_with_none():
    reader = _Reader()

    _process(_scheduler_handler(reader), {})

    assert reader.seen is None


def test_override_does_not_leak_into_reader_kwargs():
    reader = _Reader()

    _process(_scheduler_handler(reader), {"custom_extract_prompt": {"chat": "x"}})

    assert "custom_extract_prompt" not in json.dumps(reader.kwargs, default=str)
