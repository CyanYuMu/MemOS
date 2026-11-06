"""Golden set for the version chain: scripted conversations, recorded LLM answers, expected graph.

Each line of ``cases.jsonl`` is one scenario. A step is one memory arriving through the
conflict path; ``llm`` lists the recorded answers of the relation detector (and of the
fusion call when a pair is redundant), consumed in order. Only the deterministic part
(time ordering, version chain, archiving, edges, field mirroring) is being measured;
whether a real model labels a pair correctly is not.

Run ``python -m tests.memories.textual.golden.runner`` for the metrics table.
"""

from __future__ import annotations

import json

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from memos.memories.textual.item import TextualMemoryItem, TreeNodeTextualMemoryMetadata
from memos.memories.textual.tree_text_memory.organize.handler import NodeHandler
from tests.memories.textual.fake_graph import FakeGraph


CASES = Path(__file__).with_name("cases.jsonl")
USER = "golden"
MIRRORED = ("status", "history", "version", "valid_from", "valid_to", "memory_form")


class RecordedLLM:
    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.calls = 0

    def generate(self, prompt):
        self.calls += 1
        return self.answers.pop(0) if self.answers else "independent"


def load_cases(path: Path = CASES) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _acyclic(edges: list[tuple[str, str]]) -> bool:
    graph: dict[str, list[str]] = {}
    for src, dst in edges:
        graph.setdefault(src, []).append(dst)
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        if state.get(node) == 1:
            return False
        if state.get(node) == 2:
            return True
        state[node] = 1
        ok = all(visit(nxt) for nxt in graph.get(node, []))
        state[node] = 2
        return ok

    return all(visit(n) for n in list(graph))


def _add(handler: NodeHandler, node: TextualMemoryItem) -> None:
    """The per-node loop of ``GraphStructureReorganizer.handle_add``."""
    for node_a, existing, relation in handler.detect(
        node, scope=node.metadata.memory_type, user_name=USER
    ):
        archived = handler.resolve(node_a, existing, relation, user_name=USER)
        if archived == node.id:
            break


def build_graph(steps: list[dict[str, Any]]) -> tuple[FakeGraph, RecordedLLM, list[str]]:
    """Replay ``steps`` through the conflict path; returns the graph, the LLM and the node ids."""
    graph = FakeGraph()
    llm = RecordedLLM([a for step in steps for a in step.get("llm", [])])
    embedder = MagicMock()
    embedder.embed.return_value = [[0.5]]
    handler = NodeHandler(graph, llm, embedder)
    ids: list[str] = []
    for index, step in enumerate(steps):
        meta: dict[str, Any] = {
            "memory_type": step.get("memory_type", "LongTermMemory"),
            "chat_time": step.get("chat_time"),
            "created_at": f"2026-10-21T00:{index:02d}:00+00:00",
            "embedding": [0.5],
            "status": "activated",
        }
        if step.get("form") == "event":
            meta["memory_form"] = "event"
            meta["event_time_start"] = step.get("event_time_start") or step.get("chat_time")
            if step.get("event_time_end"):
                meta["event_time_end"] = step["event_time_end"]
        node = TextualMemoryItem(
            memory=step["text"], metadata=TreeNodeTextualMemoryMetadata(**meta)
        )
        graph.seed(node)
        ids.append(node.id)
        for src, dst in step.get("pre_edges", []):
            graph.add_edge(ids[src], ids[dst], "MERGED_TO")
        graph.neighbors[(0.5,)] = [i for i in ids[:-1] if graph.status(i) == "activated"]
        _add(handler, node)
    return graph, llm, ids


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    graph, llm, _ = build_graph(case["steps"])
    expect = case["expect"]
    active = {n["memory"]: i for i, n in graph.nodes.items() if graph.status(i) == "activated"}
    histories = {
        text: [h["memory"] for h in graph.history(node_id)] for text, node_id in active.items()
    }
    failures = []
    if set(active) != set(expect["active"]):
        failures.append(f"active {sorted(active)} != {sorted(expect['active'])}")
    for head, past in expect.get("history", {}).items():
        if histories.get(head) != past:
            failures.append(f"history of {head!r}: {histories.get(head)} != {past}")
    for text in expect.get("preserved", []):
        stored = [n["memory"] for n in graph.nodes.values()] + [
            h for i in graph.nodes for h in (x["memory"] for x in graph.history(i))
        ]
        if text not in stored:
            failures.append(f"text lost: {text!r}")
    if graph.deleted:
        failures.append(f"nodes deleted: {graph.deleted}")
    if not _acyclic([(s, d) for s, d in graph.merged_to()]):
        failures.append("MERGED_TO has a cycle")
    for node_id in graph.nodes:
        node, payload = graph.nodes[node_id]["metadata"], graph.payloads[node_id]
        for key in MIRRORED:
            if node.get(key) != payload.get(key):
                failures.append(f"{node_id}.{key} differs between graph and vector payload")

    wanted_past = [t for texts in expect.get("history", {}).values() for t in texts]
    found_past = [t for texts in histories.values() for t in texts if t in wanted_past]
    events = [s["text"] for s in case["steps"] if s.get("form") == "event"]
    return {
        "id": case["id"],
        "ok": not failures,
        "failures": failures,
        "adds": len(case["steps"]),
        "llm_calls": llm.calls,
        "past_wanted": len(wanted_past),
        "past_found": len(found_past),
        "events": len(events),
        "events_kept": len([t for t in events if t in active]),
    }


def metrics(results: list[dict[str, Any]]) -> dict[str, float]:
    def ratio(num: int, den: int) -> float:
        return round(num / den, 3) if den else 1.0

    return {
        "cases": len(results),
        "current_accuracy": ratio(sum(r["ok"] for r in results), len(results)),
        "past_recall": ratio(
            sum(r["past_found"] for r in results), sum(r["past_wanted"] for r in results)
        ),
        "event_retention": ratio(
            sum(r["events_kept"] for r in results), sum(r["events"] for r in results)
        ),
        "llm_calls_per_add": ratio(
            sum(r["llm_calls"] for r in results), sum(r["adds"] for r in results)
        ),
    }


def main() -> int:
    results = [run_case(case) for case in load_cases()]
    for r in results:
        print(("PASS " if r["ok"] else "FAIL ") + r["id"])
        for failure in r["failures"]:
            print("    " + failure)
    print(json.dumps(metrics(results), ensure_ascii=False, indent=2))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
