from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from memos.chunkers import ChunkerFactory
from memos.configs.llm import OpenAILLMConfig
from memos.embedders.factory import EmbedderFactory
from memos.llms.factory import LLMFactory
from memos.llms.openai import OpenAILLM
from memos.llms.thinking import disable_thinking
from memos.mem_reader.simple_struct import SimpleStructMemReader


def _llm(model: str, **overrides) -> OpenAILLM:
    config = OpenAILLMConfig(api_key="k", model_name_or_path=model, **overrides)
    return OpenAILLM(config)


def _extra_body(llm: OpenAILLM) -> dict | None:
    return llm._build_request_body([{"role": "user", "content": "hi"}])["extra_body"]


def test_each_model_family_gets_its_own_switch():
    qwen, deepseek = _llm("qwen3-32b"), _llm("DeepSeek-V4-Flash-内部")
    for llm in (qwen, deepseek):
        disable_thinking(llm)

    assert _extra_body(qwen) == {"enable_thinking": False}
    assert _extra_body(deepseek) == {"thinking": {"type": "disabled"}}


def test_explicit_settings_and_unknown_models_are_left_alone():
    explicit = _llm("qwen3-32b", enable_thinking=True)
    keyed = _llm("qwen3-32b", extra_body={"enable_thinking": True, "x": 1})
    thinking_only = _llm("deepseek-reasoner")
    other = _llm("gpt-4o")
    backup = _llm(
        "qwen3-32b", backup_client=True, backup_api_key="b", backup_api_base="http://b/v1"
    )
    for llm in (explicit, keyed, thinking_only, other, backup):
        disable_thinking(llm)

    assert _extra_body(explicit) == {"enable_thinking": True}
    assert _extra_body(keyed) == {"enable_thinking": True, "x": 1}
    assert _extra_body(thinking_only) is None
    assert _extra_body(other) is None
    assert _extra_body(backup) is None


def test_env_switch_restores_provider_default(monkeypatch):
    monkeypatch.setenv("MEMOS_EXTRACT_DISABLE_THINKING", "false")
    llm = _llm("qwen3-32b")

    disable_thinking(llm)

    assert _extra_body(llm) is None


def test_reader_disables_thinking_on_every_extraction_llm():
    main, general, pref = _llm("qwen3-32b"), _llm("qwen3-14b"), _llm("deepseek-chat")
    config = SimpleNamespace(
        llm="main-config",
        general_llm="general-config",
        preference_extractor_llm="preference-config",
        qwen_llm=None,
        embedder="embedder-config",
        chunker="chunker-config",
        remove_prompt_example=False,
    )
    chunker = MagicMock()
    chunker.config.save_rawfile = False

    with (
        patch.object(LLMFactory, "from_config", side_effect=[main, general, pref]),
        patch.object(EmbedderFactory, "from_config", return_value=MagicMock()),
        patch.object(ChunkerFactory, "from_config", return_value=chunker),
    ):
        SimpleStructMemReader(config)

    assert _extra_body(main) == {"enable_thinking": False}
    assert _extra_body(general) == {"enable_thinking": False}
    assert _extra_body(pref) == {"thinking": {"type": "disabled"}}
