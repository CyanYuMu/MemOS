"""Public distribution must not expose the SkillMemory pipeline."""

import importlib.util

from memos.mem_reader import prompt_registry


def test_public_prompt_registry_has_no_skill_extraction():
    assert not any(key.startswith("extract.skill_") for key in prompt_registry.PROMPT_KEYS)


def test_public_distribution_excludes_skill_reader():
    assert importlib.util.find_spec("memos.mem_reader.read_skill_memory") is None
