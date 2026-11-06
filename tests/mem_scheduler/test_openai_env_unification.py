from unittest.mock import patch

from memos.mem_scheduler.analyzer.eval_analyzer import EvalAnalyzer


def _no_model_configured(monkeypatch):
    for key in ("MEMSCHEDULER_MODEL", "MEMREADER_GENERAL_MODEL", "MEMRADER_MODEL"):
        monkeypatch.delenv(key, raising=False)


def test_eval_analyzer_uses_unified_openai_endpoint_env(monkeypatch, tmp_path):
    _no_model_configured(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "unified-key")
    monkeypatch.setenv("OPENAI_API_BASE", "https://unified.example/v1")
    legacy_prefix = "MEMSCHEDULER_OPENAI_"
    monkeypatch.setenv(legacy_prefix + "API_KEY", "legacy-key")
    monkeypatch.setenv(legacy_prefix + "BASE_URL", "https://legacy.example/v1")

    with patch("memos.mem_scheduler.analyzer.eval_analyzer.OpenAI") as openai:
        analyzer = EvalAnalyzer(output_dir=str(tmp_path))

    openai.assert_called_once_with(
        api_key="unified-key",
        base_url="https://unified.example/v1",
    )
    assert analyzer.openai_model == "gpt-4o-mini"


def test_eval_analyzer_takes_model_from_scheduler_env(monkeypatch, tmp_path):
    """2.0.32 起调度器的模型开关是 ``MEMSCHEDULER_MODEL``，
    旧的 ``MEMSCHEDULER_OPENAI_DEFAULT_MODEL`` 已不再被读取。"""
    monkeypatch.setenv("OPENAI_API_KEY", "unified-key")
    monkeypatch.setenv("OPENAI_API_BASE", "https://unified.example/v1")
    monkeypatch.setenv("MEMSCHEDULER_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("MEMSCHEDULER_OPENAI_DEFAULT_MODEL", "ignored-model")

    with patch("memos.mem_scheduler.analyzer.eval_analyzer.OpenAI"):
        analyzer = EvalAnalyzer(output_dir=str(tmp_path))

    assert analyzer.openai_model == "gpt-4.1-mini"


def test_eval_analyzer_falls_back_to_general_model(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "unified-key")
    monkeypatch.setenv("MEMREADER_GENERAL_MODEL", "general-model")

    with patch("memos.mem_scheduler.analyzer.eval_analyzer.OpenAI"):
        analyzer = EvalAnalyzer(output_dir=str(tmp_path))

    assert analyzer.openai_model == "general-model"


def test_qwen_model_switches_to_qwen_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_API_BASE", "https://openai.example/v1")
    monkeypatch.setenv("QWEN_API_KEY", "qwen-key")
    monkeypatch.setenv("QWEN_API_BASE", "https://qwen.example/v1")
    monkeypatch.setenv("MEMSCHEDULER_MODEL", "qwen-max")

    with patch("memos.mem_scheduler.analyzer.eval_analyzer.OpenAI") as openai:
        EvalAnalyzer(output_dir=str(tmp_path))

    openai.assert_called_once_with(
        api_key="qwen-key",
        base_url="https://qwen.example/v1",
    )
