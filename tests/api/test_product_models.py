"""Unit tests for API request-model OpenAPI schemas.

These tests lock the OpenAPI schema behaviour of request models so the
interactive docs (``/docs``) stay consistent with the documented contract.

Regression guard for issue #1505: the ``/product/add`` example must render
``messages`` as a structured message list instead of a bare ``"string"``.
Because ``messages`` is typed as ``str | MessageList | RawMessageList``, Swagger
UI would otherwise pick the leading ``str`` branch of the ``anyOf`` and show
``"messages": "string"``, which misleads users into sending plain text.
"""

import pytest

from pydantic import ValidationError

from memos.api.product_models import APIADDRequest, APISearchRequest


def test_search_top_k_defaults_to_cloud_cap():
    """Default recall size follows the cloud contract (9)."""
    assert APISearchRequest(query="q", user_id="u").top_k == 9


def test_search_top_k_rejects_more_than_25():
    """Cloud caps the mixed recall set at 25 entries."""
    with pytest.raises(ValidationError):
        APISearchRequest(query="q", user_id="u", top_k=26)


def test_search_top_k_accepts_25():
    assert APISearchRequest(query="q", user_id="u", top_k=25).top_k == 25



def test_add_request_exposes_model_level_example():
    """APIADDRequest must ship a model-level example for the interactive docs."""
    schema = APIADDRequest.model_json_schema()

    assert "example" in schema, "APIADDRequest should define a model-level example"


def test_add_request_example_messages_is_structured_list():
    """The example's ``messages`` must be a non-empty list of role/content items."""
    example = APIADDRequest.model_json_schema()["example"]

    messages = example.get("messages")
    assert isinstance(messages, list), "messages example must be a list, not a bare string"
    assert messages, "messages example should not be empty"

    first = messages[0]
    assert first.get("role"), "each example message needs a role"
    assert first.get("content"), "each example message needs content"


def test_add_request_example_covers_core_fields():
    """The example should be a copy-paste-ready payload for the core add flow."""
    example = APIADDRequest.model_json_schema()["example"]

    assert "user_id" in example
    assert "writable_cube_ids" in example
