"""Run one Dream stage against draft memories, without touching any store.

The prompt trial run must execute the same stage code the scheduler executes —
``MotiveFormation``, ``ConsolidationReasoning``, ``DreamContextualizer`` — with
only the template swapped. The stages read memory content through
``context["shared"]["graph_db"]`` and call ``context["shared"]["llm"]``; the
runner supplies an in-memory node table for the former and a recording wrapper
around the real model for the latter.

Nothing here writes memory, graph, vector, Redis, or a prompt version.
"""

from __future__ import annotations

import json

from dataclasses import dataclass
from typing import Any

from memos.dream.contextualization import DreamContextGroup, DreamContextSource, DreamContextualizer
from memos.dream.pipeline.motive import MotiveFormation
from memos.dream.pipeline.reasoning import ConsolidationReasoning
from memos.dream.types import (
    DreamCluster,
    DreamMotive,
    DreamSignalSnapshot,
    MotiveType,
)
from memos.log import get_logger


logger = get_logger(__name__)

DREAM_KEYS: tuple[str, ...] = (
    "dream.motive",
    "dream.reasoning",
    "dream.context_binding",
    "dream.context_summary",
)
DRY_RUN_CUBE = "dream-dry-run"


class _MemoryTable:
    """The subset of ``graph_db`` the Dream stages read."""

    def __init__(self, memories: list[dict[str, Any]]) -> None:
        self._by_id = {
            str(item.get("id")): {"id": str(item.get("id")), "memory": str(item.get("memory", ""))}
            for item in memories
            if item.get("id")
        }

    def get_nodes(self, ids: list[str], user_name: str | None = None) -> list[dict[str, Any]]:
        return [self._by_id[mem_id] for mem_id in ids if mem_id in self._by_id]


class _RecordingLLM:
    """Forward to the real model and keep the last raw reply."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm
        self.responses: list[str] = []

    def generate(self, *args: Any, **kwargs: Any) -> str:
        text = self._llm.generate(*args, **kwargs)
        self.responses.append(text)
        return text

    def __getattr__(self, item: str) -> Any:
        return getattr(self._llm, item)

    @property
    def last_response(self) -> str:
        return self.responses[-1] if self.responses else ""


@dataclass(frozen=True)
class DreamRun:
    """What one stage produced, shaped for the trial-run response."""

    key: str
    output: Any
    raw_output: str
    parse_ok: bool
    fallback_triggered: bool


def _parses_as(raw: str, expected: type) -> bool:
    try:
        return isinstance(
            json.loads(raw.strip().removeprefix("```json").removesuffix("```")), expected
        )
    except (ValueError, TypeError):
        return False


def _sources(memories: list[dict[str, Any]]) -> list[DreamContextSource]:
    return [
        DreamContextSource(
            id=str(item.get("id")),
            memory=str(item.get("memory", "")),
            metadata={},
            dream={},
        )
        for item in memories
        if item.get("id")
    ]


def _run_motive(recorder: _RecordingLLM, memories: list[dict[str, Any]], max_motives: int):
    snapshot = DreamSignalSnapshot(
        mem_cube_id=DRY_RUN_CUBE,
        pending_memory_ids=[str(item.get("id")) for item in memories if item.get("id")],
    )
    formation = MotiveFormation(max_motives=max_motives)
    formation.bind_context({"shared": {"llm": recorder, "graph_db": _MemoryTable(memories)}})
    clusters = formation.form(signal_snapshot=snapshot, text_mem=None, cube_id=DRY_RUN_CUBE)
    output = [cluster.motive.model_dump(mode="json") for cluster in clusters]
    fallback = any(str(item.get("description", "")).startswith("Fallback:") for item in output)
    return output, fallback


def _run_reasoning(recorder: _RecordingLLM, memories: list[dict[str, Any]]):
    ids = [str(item.get("id")) for item in memories if item.get("id")]
    cluster = DreamCluster(
        cluster_id="cluster:dry-run",
        motive=DreamMotive(
            motive_id="motive:dry-run",
            motive_type=MotiveType.NEWNESS,
            description="Dry run: reason across these memories.",
            memory_ids=ids,
        ),
    )
    reasoning = ConsolidationReasoning()
    reasoning.bind_context({"shared": {"llm": recorder, "graph_db": _MemoryTable(memories)}})
    results = reasoning.reason(clusters=[cluster], text_mem=None, cube_id=DRY_RUN_CUBE)
    actions = [action.model_dump(mode="json") for result in results for action in result.actions]
    fallback = any(str(action.get("rationale", "")).startswith("Fallback:") for action in actions)
    output = actions[0] if len(actions) == 1 else actions
    return output, fallback


def _run_context_binding(recorder: _RecordingLLM, memories: list[dict[str, Any]]):
    contextualizer = DreamContextualizer(binding_min_group_size=1, binding_max_group_size=100)
    contextualizer.bind_context({"shared": {"llm": recorder, "graph_db": _MemoryTable(memories)}})
    groups = contextualizer._llm_bind_candidate_pool(
        llm=recorder, pool_key="dry-run", memories=_sources(memories)
    )
    output = [
        {
            "key": group.proposed_key or group.group_key,
            "ids": group.memory_ids,
            "confidence": group.confidence,
            "strategy": group.strategy,
        }
        for group in groups
    ]
    fallback = any(group.strategy != "llm" for group in groups)
    return output, fallback


def _run_context_summary(recorder: _RecordingLLM, memories: list[dict[str, Any]]):
    contextualizer = DreamContextualizer()
    contextualizer.bind_context({"shared": {"llm": recorder, "graph_db": _MemoryTable(memories)}})
    group = DreamContextGroup(group_key="dry-run", memories=_sources(memories))
    key, memory, confidence, strategy = contextualizer._summarize_group(group=group)
    output = {"key": key, "memory": memory, "confidence": confidence}
    return output, strategy != "llm"


_RUNNERS = {
    "dream.motive": _run_motive,
    "dream.reasoning": _run_reasoning,
    "dream.context_binding": _run_context_binding,
    "dream.context_summary": _run_context_summary,
}


def run_dream_stage(
    key: str,
    memories: list[dict[str, Any]],
    *,
    llm: Any,
    max_motives: int = 3,
) -> DreamRun:
    """Run one Dream stage for ``key`` and report whether its output parsed."""
    if key not in _RUNNERS:
        raise ValueError(f"{key!r} is not a Dream prompt")
    recorder = _RecordingLLM(llm)
    if key == "dream.motive":
        output, fallback = _run_motive(recorder, memories, max_motives)
    else:
        output, fallback = _RUNNERS[key](recorder, memories)

    raw = recorder.last_response or ""
    parse_ok = _parses_as(raw, dict) or _parses_as(raw, list)
    if not parse_ok and output:
        # The stage salvaged something from a non-JSON reply; the reply itself
        # is what the console shows as "the model broke the contract".
        logger.info("[DreamDryRun] %s produced output from a non-JSON reply", key)
    return DreamRun(
        key=key,
        output=output,
        raw_output=raw,
        parse_ok=parse_ok,
        fallback_triggered=fallback,
    )
