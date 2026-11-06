from __future__ import annotations

import json
import logging
import re

from memos.dream.prompts.motive_prompt import MOTIVE_FORMATION_PROMPT, MOTIVE_OUTPUT_SUFFIX
from memos.dream.types import DreamCluster, DreamMotive, MotiveType
from memos.mem_reader.prompt_runtime import prompt_for


logger = logging.getLogger(__name__)


class MotiveFormation:
    """LLM-powered Dream motive formation stage.

    Reads pending memory content, asks the LLM to identify which groups are
    worth consolidating, and allows the LLM to decline (return empty) if
    nothing merits a Dream run.

    Falls back to a simple single-cluster heuristic if LLM is unavailable.
    """

    def __init__(self, *, max_motives: int = 3) -> None:
        self.max_motives = max_motives

    def bind_context(self, context: dict) -> None:
        self.context = context

    def form(self, *, signal_snapshot, text_mem, cube_id: str) -> list[DreamCluster]:
        if not signal_snapshot.pending_memory_ids:
            return []

        memories = self._fetch_memory_content(signal_snapshot.pending_memory_ids, text_mem, cube_id)
        if not memories:
            return self._fallback(signal_snapshot)

        llm = self.context.get("shared", {}).get("llm")
        if llm is None:
            logger.info("[Dream] LLM unavailable; using fallback motive formation.")
            return self._fallback(signal_snapshot)

        return self._llm_form(llm, memories, signal_snapshot)

    def _llm_form(self, llm, memories: list[dict], signal_snapshot) -> list[DreamCluster]:
        memories_block = "\n".join(f"- [{m['id']}] {m['content'][:8000]}" for m in memories)
        template = prompt_for("dream.motive", "en", MOTIVE_FORMATION_PROMPT)
        try:
            prompt = template.format(
                memories_block=memories_block,
                max_motives=self.max_motives,
            )
        except (KeyError, IndexError, ValueError):
            logger.warning(
                "[Dream] project prompt for dream.motive does not render; using the built-in one."
            )
            prompt = MOTIVE_FORMATION_PROMPT.format(
                memories_block=memories_block,
                max_motives=self.max_motives,
            )

        prompt += MOTIVE_OUTPUT_SUFFIX

        try:
            response = llm.generate([{"role": "user", "content": prompt}])
            motives_raw = self._parse_motives_json(response)
        except Exception:
            logger.exception("[Dream] LLM motive formation failed; using fallback.")
            return self._fallback(signal_snapshot)

        if not isinstance(motives_raw, list) or len(motives_raw) == 0:
            logger.info("[Dream] LLM decided nothing is worth dreaming about.")
            return []

        clusters = []
        for raw in motives_raw[: self.max_motives]:
            motive = DreamMotive(
                motive_id=raw.get("motive_id", f"motive:{signal_snapshot.mem_cube_id}"),
                motive_type=MotiveType.NEWNESS,
                description=raw.get("description", ""),
                memory_ids=raw.get("memory_ids", []),
            )
            clusters.append(
                DreamCluster(
                    cluster_id=f"cluster:{motive.motive_id}",
                    motive=motive,
                )
            )
        return clusters

    @staticmethod
    def _parse_motives_json(text: str) -> list:
        """Tolerantly extract the motives JSON array from an LLM response.

        Handles: raw JSON, ```json fences (any case/whitespace), leading or
        trailing prose, and a bare object (wrapped into a list).
        """
        text = (text or "").strip()
        try:
            return json.loads(text)
        except Exception:
            pass
        fence = re.search(r"```(?:json|JSON)?\s*(.*?)```", text, re.S)
        if fence:
            try:
                return json.loads(fence.group(1).strip())
            except Exception:
                pass
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                pass
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return [json.loads(text[start : end + 1])]
        raise ValueError("no parseable JSON array in LLM response")

    def _fetch_memory_content(self, memory_ids: list[str], text_mem, cube_id: str) -> list[dict]:
        """Retrieve actual memory text for motive analysis.

        Results are re-ordered to match the input ``memory_ids`` sequence
        because graph DB ``get_nodes`` does not guarantee return order.
        """
        graph_db = self.context.get("shared", {}).get("graph_db")
        if graph_db is None:
            return []

        try:
            nodes = graph_db.get_nodes(memory_ids, user_name=cube_id)
        except Exception:
            logger.warning("[Dream] failed to fetch memory content for motive analysis.")
            return []

        node_by_id: dict[str, dict] = {}
        for node in nodes:
            if isinstance(node, dict):
                node_id = node.get("id", "")
                if node_id:
                    node_by_id[node_id] = {
                        "id": node_id,
                        "content": node.get("memory", "") or node.get("content", ""),
                    }

        return [node_by_id[mid] for mid in memory_ids if mid in node_by_id]

    def _fallback(self, signal_snapshot) -> list[DreamCluster]:
        """Simple single-cluster fallback when LLM is unavailable."""
        motive = DreamMotive(
            motive_id=f"motive:{signal_snapshot.mem_cube_id}",
            motive_type=MotiveType.NEWNESS,
            description="Fallback: pending memories accumulated without LLM analysis.",
            memory_ids=list(signal_snapshot.pending_memory_ids),
        )
        return [
            DreamCluster(
                cluster_id=f"cluster:{signal_snapshot.mem_cube_id}",
                motive=motive,
            )
        ]
