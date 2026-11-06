"""Dream stages take their prompt from the registry, and the S8 live check.

Offline tests use a stub model that records the prompt; the live test saves a
marked ``dream.motive`` template and checks the marker shows up in the motive
stage's real output.
"""

import os

import pytest

from memos.dream.dry_run import run_dream_stage
from memos.mem_reader import prompt_store, trial
from memos.mem_reader.prompt_registry import default_body, get_spec
from memos.mem_reader.prompt_runtime import prompt_scope


CUBE = "cube-dream"
MEMORIES = [
    {"id": "m1", "memory": "用户每次提旅行，妈妈都婉拒。"},
    {"id": "m2", "memory": "妈妈说最近腰酸背痛，只想在家浇花。"},
    {"id": "m3", "memory": "用户周末在准备三亚攻略。"},
]


class RecordingLLM:
    def __init__(self, reply: str):
        self.reply = reply
        self.prompts: list[str] = []

    def generate(self, messages, **_kwargs):
        self.prompts.append(messages[0]["content"])
        return self.reply


@pytest.fixture(autouse=True)
def prompt_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "dream.db"))


def test_motive_stage_uses_the_project_prompt():
    llm = RecordingLLM('[{"motive_id": "m", "description": "d", "memory_ids": ["m1"]}]')
    prompt_store.save_slot(CUBE, "dream.motive", "en", body="动机模板 memories={memories_block}")
    with prompt_scope(CUBE):
        run = run_dream_stage("dream.motive", MEMORIES, llm=llm)
    assert llm.prompts[-1].startswith("动机模板 memories=")
    # The locked suffix is appended after whatever the user wrote.
    assert llm.prompts[-1].endswith("Be concise.")
    assert run.output == [
        {"motive_id": "m", "motive_type": "newness", "description": "d", "memory_ids": ["m1"]}
    ]
    assert run.parse_ok is True
    assert run.fallback_triggered is False


def test_reasoning_stage_uses_the_project_prompt():
    llm = RecordingLLM('{"dream_content": "洞察", "hypothetical_question": "问？"}')
    prompt_store.save_slot(
        CUBE,
        "dream.reasoning",
        "en",
        body=("推理模板 {motive_description} {source_memories_block} {related_memories_block}"),
    )
    with prompt_scope(CUBE):
        run = run_dream_stage("dream.reasoning", MEMORIES, llm=llm)
    assert llm.prompts[-1].startswith("推理模板 Dry run")
    assert run.output["new_content"] == "洞察"
    assert run.fallback_triggered is False


def test_context_binding_stage_uses_the_project_prompt():
    llm = RecordingLLM('{"contexts": [{"key": "家庭", "ids": ["m1", "m2"], "confidence": 0.8}]}')
    prompt_store.save_slot(CUBE, "dream.context_binding", "en", body="绑定模板 {memories_block}")
    with prompt_scope(CUBE):
        run = run_dream_stage("dream.context_binding", MEMORIES, llm=llm)
    assert llm.prompts[-1].startswith("绑定模板 ")
    assert run.output[0]["confidence"] == 0.8


def test_context_summary_stage_uses_the_project_prompt():
    llm = RecordingLLM('{"key": "家庭", "memory": "关于家庭的上下文", "confidence": 0.7}')
    prompt_store.save_slot(
        CUBE, "dream.context_summary", "en", body="摘要模板 {existing_key} {memories_block}"
    )
    with prompt_scope(CUBE):
        run = run_dream_stage("dream.context_summary", MEMORIES, llm=llm)
    assert llm.prompts[-1].startswith("摘要模板 ")
    assert run.output["key"] == "家庭"


def test_a_broken_dream_prompt_falls_back_to_the_built_in_one():
    llm = RecordingLLM('{"dream_content": "洞察"}')
    prompt_store.save_slot(
        CUBE,
        "dream.reasoning",
        "en",
        body="推理 {motive_description} {source_memories_block} {related_memories_block}",
    )
    conn = __import__("sqlite3").connect(str(prompt_store._db_path()))
    with conn:
        conn.execute(
            "UPDATE prompt_versions SET body = ? WHERE prompt_key = 'dream.reasoning'",
            ("推理模板 {missing_field}",),
        )
    conn.close()
    with prompt_scope(CUBE):
        run = run_dream_stage("dream.reasoning", MEMORIES, llm=llm)
    assert llm.prompts[-1].startswith(default_body("dream.reasoning", "en")[:40])
    assert run.parse_ok is True


def test_unknown_dream_key_is_rejected():
    with pytest.raises(ValueError):
        run_dream_stage("dream.nope", MEMORIES, llm=RecordingLLM("[]"))


@pytest.mark.skipif(
    not os.getenv("MEMRADER_API_KEY"), reason="real model test: set MEMRADER_API_KEY"
)
def test_saved_motive_prompt_shows_up_in_the_motive_stage():
    from memos.api.config import APIConfig
    from memos.configs.llm import LLMConfigFactory
    from memos.llms.factory import LLMFactory

    marker = "MOTIVEMARK-5C21"
    body = (
        default_body("dream.motive", "en")
        + f"\n\nEvery motive description must end with the exact text {marker}."
    )
    prompt_store.save_slot(CUBE, "dream.motive", "en", body=body)

    llm = LLMFactory.from_config(LLMConfigFactory.model_validate(APIConfig.get_memreader_config()))
    with prompt_scope(CUBE):
        run = run_dream_stage("dream.motive", MEMORIES, llm=llm)

    assert run.parse_ok, run.raw_output[:400]
    assert run.output, "the model produced no motive"
    assert any(marker in str(item.get("description", "")) for item in run.output), run.output

    prompt_store.reset_slots(CUBE, key="dream.motive")
    with prompt_scope(CUBE):
        after = run_dream_stage("dream.motive", MEMORIES, llm=llm)
    assert not any(marker in str(item.get("description", "")) for item in after.output)


def test_dream_scheduler_entry_scopes_prompts_to_the_cube(monkeypatch):
    from types import SimpleNamespace

    from memos.mem_reader.prompt_runtime import prompt_for
    from memos.mem_scheduler.task_schedule_modules.handlers import mem_dream_handler

    prompt_store.save_slot(
        "cube-dream-entry", "dream.motive", "en", body="入口模板 {memories_block}"
    )
    seen: list[str] = []

    def fake_hook(_hook, **_kwargs):
        seen.append(prompt_for("dream.motive", "en", "内置"))

    monkeypatch.setattr(mem_dream_handler, "trigger_single_hook", fake_hook)
    handler = mem_dream_handler.MemDreamMessageHandler(SimpleNamespace())
    handler._run_dream(
        user_id="u1",
        mem_cube_id="cube-dream-entry",
        user_name="user-name-of-u1",
        signal_snapshot=None,
        text_mem=object(),
    )
    assert seen == ["入口模板 {memories_block}"]


def _trial(key: str, reply: str) -> trial.TrialRun:
    return trial.run_trial(
        get_spec(key),
        label="draft",
        lang="en",
        body=None,
        input_data={"memories": MEMORIES},
        cube_id=CUBE,
        llm=RecordingLLM(reply),
    )


def test_reasoning_trial_checks_the_contract_against_the_model_reply():
    # The stage output is a DreamAction (``new_content``), not the model JSON that
    # the contract describes; a reply that follows the contract must pass.
    run = _trial("dream.reasoning", '{"dream_content": "洞察", "hypothetical_question": "问？"}')
    assert run.output["new_content"] == "洞察"
    assert (run.contract_ok, run.missing) == (True, [])


def test_reasoning_trial_still_flags_a_reply_without_dream_content():
    run = _trial("dream.reasoning", '{"hypothetical_question": "问？"}')
    assert (run.contract_ok, run.missing) == (False, ["dream_content"])


def test_reasoning_trial_flags_a_reply_that_is_not_json():
    run = _trial("dream.reasoning", "这不是 JSON")
    assert run.fallback_triggered is True
    assert run.contract_ok is False


def test_context_binding_trial_passes_when_the_reply_has_contexts():
    reply = (
        '{"contexts": [{"key": "家庭", "ids": ["m1"], "confidence": 0.8, "reason": "同一件事"}]}'
    )
    run = _trial("dream.context_binding", reply)
    assert (run.contract_ok, run.missing) == (True, [])


def test_dream_contract_is_checked_before_the_raw_reply_is_truncated(monkeypatch):
    monkeypatch.setattr(trial, "MAX_RAW_OUTPUT_CHARS", 20)
    run = _trial("dream.reasoning", '{"dream_content": "洞察", "hypothetical_question": "问？"}')
    assert run.truncated is True
    assert run.contract_ok is True
