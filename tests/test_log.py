import logging
import os

from types import SimpleNamespace

from dotenv import load_dotenv

from memos import log


load_dotenv()


def generate_trace_id() -> str:
    """Generate a random trace_id."""
    return os.urandom(16).hex()


def test_setup_logfile_creates_file(tmp_path, monkeypatch):
    monkeypatch.setattr("memos.settings.MEMOS_DIR", tmp_path)
    path = log._setup_logfile()
    assert path.exists()
    assert path.name == "memos.log"


def test_get_logger_returns_logger():
    logger = log.get_logger("test_logger")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "test_logger"
    assert any(isinstance(h, logging.StreamHandler) for h in logger.parent.handlers) or any(
        isinstance(h, logging.FileHandler) for h in logger.parent.handlers
    )


def test_get_logger_configures_logging_once_per_process(monkeypatch):
    calls = []

    monkeypatch.setattr(log, "_LOGGING_CONFIGURED_PID", None)
    monkeypatch.setattr(log, "_get_current_pid", lambda: 123)
    monkeypatch.setattr(log, "dictConfig", lambda config: calls.append(config))

    log.get_logger("first")
    log.get_logger("second")

    assert len(calls) == 1


def test_get_logger_reconfigures_after_process_fork(monkeypatch):
    calls = []
    pid = 123

    def getpid():
        return pid

    monkeypatch.setattr(log, "_LOGGING_CONFIGURED_PID", None)
    monkeypatch.setattr(log, "_get_current_pid", getpid)
    monkeypatch.setattr(log, "dictConfig", lambda config: calls.append(config))

    log.get_logger("parent")
    pid = 456
    log.get_logger("child")

    assert len(calls) == 2


def test_summarize_search_request_uses_metadata_and_query_hash_only():
    request = SimpleNamespace(
        query="private query content",
        user_id="private-user",
        mode="fast",
        readable_cube_ids=["cube-a", "cube-b"],
        include_memory_view=["detail_factual", "preference"],
        memory_limit_number=6,
        dedup="mmr",
        rerank=True,
    )

    summary = log.summarize_search_request(request)

    assert summary["query_chars"] == len(request.query)
    assert len(summary["query_hash"]) == 16
    assert summary["mode"] == "fast"
    assert summary["readable_cube_count"] == 2
    assert summary["views"] == ["detail_factual", "preference"]
    assert summary["top_k"] == 6
    assert summary["dedup"] == "mmr"
    assert summary["rerank"] is True
    assert request.query not in str(summary)
    assert request.user_id not in str(summary)


def test_summarize_search_results_only_counts_buckets_and_items():
    results = {
        "text_mem": [
            {
                "cube_id": "cube-a",
                "memories": [
                    {
                        "memory": "private memory value",
                        "metadata": {
                            "embedding": [0.1] * 256,
                            "properties": {"secret": 1},
                        },
                    },
                    {"memory": "another private value"},
                ],
            }
        ],
        "pref_mem": [{"cube_id": "cube-a", "memories": [{"memory": "private preference"}]}],
        "aux_mem": [],
        "pref_note": "private preference note",
    }

    summary = log.summarize_search_results(results)

    assert summary == {
        "bucket_counts": {"text_mem": 1, "pref_mem": 1, "aux_mem": 0},
        "item_counts": {"text_mem": 2, "pref_mem": 1, "aux_mem": 0},
        "total_items": 3,
    }
    rendered = str(summary)
    assert "private memory value" not in rendered
    assert "0.1" not in rendered
    assert "secret" not in rendered
    assert "private preference note" not in rendered
    assert len(rendered) <= len(str(results)) * 0.2


def test_summarize_textual_memories_only_counts_types():
    memories = [
        SimpleNamespace(
            memory="private long-term memory",
            metadata=SimpleNamespace(
                memory_type="LongTermMemory",
                embedding=[0.1, 0.2],
            ),
        ),
        SimpleNamespace(
            memory="private user memory",
            metadata=SimpleNamespace(
                memory_type="UserMemory",
                properties={"secret": True},
            ),
        ),
    ]

    summary = log.summarize_textual_memories(memories)

    assert summary == {
        "total_items": 2,
        "type_counts": {"LongTermMemory": 1, "UserMemory": 1},
    }
    rendered = str(summary)
    assert "private long-term memory" not in rendered
    assert "0.1" not in rendered
    assert "secret" not in rendered


def test_summarize_add_request_keeps_only_the_shape_of_the_conversation():
    """这条取代了 `add_req.model_dump_json(indent=2)`，后者把整段对话打进日志。"""
    request = SimpleNamespace(
        mem_cube_id="example-agent",
        writable_cube_ids=["example-agent"],
        async_mode="sync",
        mode="fine",
        messages=[{"role": "user", "content": "私密的告警正文"}],
        chat_history=[{"role": "assistant", "content": "私密的结论"}],
        memory_content="私密记忆",
        custom_tags=["sample-alert"],
        doc_path=None,
        is_feedback=False,
    )

    summary = log.summarize_add_request(request)

    assert summary["mem_cube_id"] == "example-agent"
    assert summary["sync_mode"] == "sync"
    assert summary["mode"] == "fine"
    assert summary["message_count"] == 1
    assert summary["chat_history_count"] == 1
    assert summary["memory_content_chars"] == len("私密记忆")
    assert summary["custom_tag_count"] == 1
    assert summary["has_doc_path"] is False
    assert len(summary["message_hash"]) == 16
    rendered = str(summary)
    assert "私密的告警正文" not in rendered
    assert "私密的结论" not in rendered
    assert "私密记忆" not in rendered


def test_summarize_add_request_survives_an_empty_request():
    summary = log.summarize_add_request(SimpleNamespace())

    assert summary["message_count"] == 0
    assert summary["message_chars"] == 2  # json.dumps([])
    assert summary["memory_content_chars"] == 0


def test_preview_text_leaves_a_short_value_alone():
    assert log.preview_text("短文本") == "短文本"


def test_preview_text_clamps_a_runaway_llm_answer():
    """实测有一次 `<answer>` 里返回了约 173 MB 的反斜杠，原样写成了一条日志记录。"""
    text = "\\" * 173_000_000

    preview = log.preview_text(text)

    assert len(preview) < 300
    assert preview.endswith("<truncated, 173000000 chars total>")


def test_console_level_defaults_to_warning(monkeypatch):
    monkeypatch.delenv("MEMOS_LOG_LEVEL", raising=False)
    monkeypatch.setattr("memos.settings.DEBUG", False)

    assert log._resolve_console_level() == logging.WARNING


def test_console_level_follows_debug_setting_when_unset(monkeypatch):
    monkeypatch.delenv("MEMOS_LOG_LEVEL", raising=False)
    monkeypatch.setattr("memos.settings.DEBUG", True)

    assert log._resolve_console_level() == logging.DEBUG


def test_console_level_can_be_raised_by_env(monkeypatch):
    monkeypatch.setattr("memos.settings.DEBUG", False)
    monkeypatch.setenv("MEMOS_LOG_LEVEL", "INFO")

    assert log._resolve_console_level() == logging.INFO


def test_console_level_env_is_case_insensitive(monkeypatch):
    monkeypatch.setattr("memos.settings.DEBUG", False)
    monkeypatch.setenv("MEMOS_LOG_LEVEL", " info ")

    assert log._resolve_console_level() == logging.INFO


def test_console_level_ignores_an_unusable_env_value(monkeypatch):
    monkeypatch.setattr("memos.settings.DEBUG", False)
    monkeypatch.setenv("MEMOS_LOG_LEVEL", "verbose")

    assert log._resolve_console_level() == logging.WARNING


def test_console_handler_uses_the_resolved_level():
    """The env feeds selected_log_level; the console handler must consume it."""
    assert log.LOGGING_CONFIG["handlers"]["console"]["level"] == log.selected_log_level


def test_http_client_loggers_are_pinned_to_warning():
    for name in ("httpx", "httpcore"):
        assert log.LOGGING_CONFIG["loggers"][name]["level"] == logging.WARNING


def test_configure_logging_applies_the_http_client_level():
    """dictConfig has to set the level, not merely carry it in the dict."""
    log.configure_logging(force=True)

    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        assert logger.level == logging.WARNING
        assert logger.propagate is True
