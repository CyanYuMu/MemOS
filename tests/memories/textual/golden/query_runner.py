"""Golden set for the query side: a world built by the real version chain, then time-aware queries.

Each line of ``query_cases.jsonl`` is a world (``steps`` as in ``cases.jsonl``) and the
queries asked against it. ``llm`` is the recorded intent answer: an object (the model's
JSON), ``"error"`` (the call fails) or ``null`` (no model configured); an optional ``relativity`` maps a memory text to the score its
hit carries. Every activated
node is treated as a retrieval hit, so what is measured is the deterministic part: the
gate, the fallback chain, the window, and which version each hit reads as.

Run ``python -m tests.memories.textual.golden.query_runner`` for the metrics table.
"""

from __future__ import annotations

import json
import os

from datetime import datetime
from pathlib import Path
from typing import Any

from memos.search import time_understanding
from memos.search.time_understanding import understand_query
from memos.search.version_select import apply_time_intent
from tests.memories.textual.golden.runner import build_graph


QUERY_CASES = Path(__file__).with_name("query_cases.jsonl")


class RecordedIntentLLM:
    def __init__(self, answer: Any):
        self.answer = answer
        self.calls = 0

    def generate(self, messages, **kwargs):
        self.calls += 1
        if self.answer == "error":
            raise RuntimeError("recorded failure")
        return json.dumps(self.answer)


def load_query_cases(path: Path = QUERY_CASES) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _hits(graph, relativity: dict[str, float] | None = None) -> dict[str, Any]:
    memories = [
        {
            "id": node_id,
            "memory": node["memory"],
            "metadata": {
                **node["metadata"],
                **(
                    {"relativity": relativity[node["memory"]]}
                    if node["memory"] in (relativity or {})
                    else {}
                ),
            },
        }
        for node_id, node in graph.nodes.items()
        if graph.status(node_id) == "activated"
    ]
    return {"text_mem": [{"cube_id": "golden", "memories": memories}]}


def run_query(graph, query_case: dict[str, Any]) -> dict[str, Any]:
    os.environ["MEMOS_QUERY_TIMEZONE"] = "UTC"
    time_understanding.clear_cache()
    answer = query_case.get("llm")
    llm = None if answer is None else RecordedIntentLLM(answer)
    now = datetime.fromisoformat(query_case["now"])

    intent = understand_query(query_case["query"], llm, now=now)
    out = apply_time_intent(_hits(graph, query_case.get("relativity")), intent, top_k=10, now=now)
    texts = [m["memory"] for m in out["text_mem"][0]["memories"]]

    expect = query_case["expect"]
    failures = []
    if intent.kind != expect["kind"]:
        failures.append(f"intent {intent.kind} != {expect['kind']}")
    if texts != expect["texts"]:
        failures.append(f"texts {texts} != {expect['texts']}")
    return {
        "id": query_case["id"],
        "ok": not failures,
        "failures": failures,
        "intent_ok": intent.kind == expect["kind"],
        "selection_ok": texts == expect["texts"],
        "llm_calls": llm.calls if llm else 0,
    }


def run_world(case: dict[str, Any]) -> list[dict[str, Any]]:
    graph, _, _ = build_graph(case["steps"])
    return [run_query(graph, q) for q in case["queries"]]


def metrics(results: list[dict[str, Any]]) -> dict[str, float]:
    def ratio(num: int, den: int) -> float:
        return round(num / den, 3) if den else 1.0

    total = len(results)
    return {
        "queries": total,
        "intent_accuracy": ratio(sum(r["intent_ok"] for r in results), total),
        "selection_accuracy": ratio(sum(r["selection_ok"] for r in results), total),
        "llm_calls_per_query": ratio(sum(r["llm_calls"] for r in results), total),
        "llm_free_share": ratio(sum(r["llm_calls"] == 0 for r in results), total),
    }


def main() -> int:
    results = [r for case in load_query_cases() for r in run_world(case)]
    for r in results:
        print(("PASS " if r["ok"] else "FAIL ") + r["id"])
        for failure in r["failures"]:
            print("    " + failure)
    print(json.dumps(metrics(results), ensure_ascii=False, indent=2))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
