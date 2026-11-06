"""Project-level prompt overrides must reach every reader.

``extract.chat`` used to reach ``simple_struct`` only, so a prompt saved from
the console was silently ignored by the default ``multimodal_struct`` reader in
production. These tests keep both readers on the same override path.
"""

from types import SimpleNamespace

from memos.mem_reader.prompt_config import save_prompt
from memos.mem_reader.prompt_runtime import prompt_scope


def _multimodal_reader(captured: list[str]):
    from memos.mem_reader.multi_modal_struct import MultiModalStructMemReader

    class _LLM:
        def generate(self, messages):
            captured.append(messages[0]["content"])
            return '{"memory list": [], "summary": ""}'

    reader = object.__new__(MultiModalStructMemReader)
    reader.config = SimpleNamespace(remove_prompt_example=False)
    reader.llm = _LLM()
    return reader


def test_multimodal_reader_uses_project_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "multimodal.db"))
    save_prompt("cube-mm", "项目模板\n${conversation}")

    captured: list[str] = []
    reader = _multimodal_reader(captured)
    with prompt_scope("cube-mm"):
        reader._get_llm_response("用户提到昨天的会议", None, None, "chat")

    assert captured[-1].startswith("项目模板")
