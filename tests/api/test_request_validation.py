"""Tests for the cloud file/token caps and multimodal async forcing."""

import pytest

from memos.api.product_models import APIADDRequest, APIExtractRequest
from memos.api.request_validation import add_limits, file_size_bytes, inspect_add_messages


def _image_message() -> list[dict]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
            ],
        }
    ]


def test_plain_text_does_not_force_async():
    req = APIADDRequest(
        user_id="u", messages=[{"role": "user", "content": "hi"}], async_mode="sync"
    )

    assert req.async_mode == "sync"


def test_multimodal_forces_async():
    req = APIADDRequest(user_id="u", messages=_image_message(), async_mode="sync")

    assert req.async_mode == "async"


def test_file_count_limit(monkeypatch):
    monkeypatch.setenv("MEMOS_MAX_FILES_PER_REQUEST", "1")

    files = [{"type": "file", "file": {"file_data": "https://example.com/a.pdf"}}] * 2

    with pytest.raises(ValueError, match="Too many files"):
        inspect_add_messages(files)


def test_file_size_limit(monkeypatch):
    monkeypatch.setenv("MEMOS_MAX_FILE_SIZE_MB", "0")

    files = [{"type": "file", "file": {"file_data": "data:text/plain;base64,aGVsbG8="}}]

    with pytest.raises(ValueError, match="File too large"):
        inspect_add_messages(files)


def test_file_page_limit(monkeypatch):
    monkeypatch.setenv("MEMOS_MAX_FILE_PAGES", "1")

    files = [{"type": "file", "file": {"file_data": "https://example.com/a.pdf", "pages": 2}}]

    with pytest.raises(ValueError, match="File too long"):
        inspect_add_messages(files)


def test_token_limit(monkeypatch):
    monkeypatch.setenv("MEMOS_MAX_ADD_TOKENS", "1")

    with pytest.raises(ValueError, match="Input too large"):
        inspect_add_messages([{"role": "user", "content": "a much longer piece of text"}])


def test_limits_are_configurable(monkeypatch):
    monkeypatch.setenv("MEMOS_MAX_FILES_PER_REQUEST", "7")
    monkeypatch.setenv("MEMOS_MAX_FILE_SIZE_MB", "3")
    monkeypatch.setenv("MEMOS_MAX_FILE_PAGES", "11")
    monkeypatch.setenv("MEMOS_MAX_ADD_TOKENS", "1234")

    limits = add_limits()

    assert limits.max_files == 7
    assert limits.max_file_size_bytes == 3 * 1024 * 1024
    assert limits.max_file_pages == 11
    assert limits.max_input_tokens == 1234


def test_inspection_reports_multimodal():
    report = inspect_add_messages(_image_message())
    assert report.has_multimodal is True
    assert report.file_count == 1


def test_base64_size_is_estimated_without_decoding():
    # "hello" -> aGVsbG8= (8 chars incl. padding) -> 5 bytes
    part = {"type": "file", "file": {"file_data": "data:text/plain;base64,aGVsbG8="}}

    assert file_size_bytes(part) == 5


def test_large_text_fails_the_token_limit_without_tokenizing(monkeypatch):
    monkeypatch.setenv("MEMOS_MAX_ADD_TOKENS", "1")

    with pytest.raises(ValueError, match="Input too large"):
        inspect_add_messages([{"role": "user", "content": "x" * 100_000}])


def test_extract_uses_the_same_limits(monkeypatch):
    monkeypatch.setenv("MEMOS_MAX_ADD_TOKENS", "1")

    with pytest.raises(ValueError, match="Input too large"):
        APIExtractRequest(
            user_id="u",
            messages=[{"role": "user", "content": "a much longer piece of text"}],
        )


def test_extract_does_not_force_async():
    req = APIExtractRequest(user_id="u", messages=_image_message())

    assert not hasattr(req, "async_mode")
