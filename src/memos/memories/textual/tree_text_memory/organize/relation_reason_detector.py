import json
import traceback

from memos.embedders.factory import OllamaEmbedder
from memos.graph_dbs.base import BaseGraphDB
from memos.graph_dbs.item import GraphDBNode
from memos.llms.base import BaseLLM
from memos.log import get_logger


logger = get_logger(__name__)


class RelationAndReasoningDetector:
    def __init__(self, graph_store: BaseGraphDB, llm: BaseLLM, embedder: OllamaEmbedder):
        self.graph_store = graph_store
        self.llm = llm
        self.embedder = embedder

    def process_node(self, node: GraphDBNode, exclude_ids: list[str], top_k: int = 5):
        """
        Unified pipeline for:
        1) Pairwise relations (cause, condition, conflict, relate)
        2) Inferred nodes
        3) Sequence links
        4) Aggregate concepts
        """
        results = {
            "relations": [],
            "inferred_nodes": [],
            "sequence_links": [],
            "aggregate_nodes": [],
        }
        try:
            if node.metadata.type == "reasoning":
                logger.info(f"Skip reasoning for inferred node {node.id}")
                return {
                    "relations": [],
                    "inferred_nodes": [],
                    "sequence_links": [],
                    "aggregate_nodes": [],
                }
            """
            nearest = self.graph_store.get_neighbors_by_tag(
                tags=node.metadata.tags,
                exclude_ids=exclude_ids,
                top_k=top_k,
                min_overlap=2,
            )
            nearest = [GraphDBNode(**cand_data) for cand_data in nearest]

            # 1) Pairwise relations (including CAUSE/CONDITION/CONFLICT)
            pairwise = self._detect_pairwise_causal_condition_relations(node, nearest)
            results["relations"].extend(pairwise["relations"])
            """

            """
            # 2) Inferred nodes (from causal/condition)
            inferred = self._infer_fact_nodes_from_relations(pairwise)
            results["inferred_nodes"].extend(inferred)
            """

            """
            3) Sequence (optional, if you have timestamps)
            seq = self._detect_sequence_links(node, nearest)
            results["sequence_links"].extend(seq)
            """

            """
            # 4) Aggregate
            agg = self._detect_aggregate_node_for_group(node, nearest, min_group_size=5)
            if agg:
                results["aggregate_nodes"].append(agg)
            """

        except Exception as e:
            logger.error(
                f"Error {e} while process struct reorganize: trace: {traceback.format_exc()}"
            )
        return results


    def _parse_json_result(self, response_text):
        try:
            response_text = response_text.replace("```", "").replace("json", "")
            response_json = json.loads(response_text)
            return response_json
        except json.JSONDecodeError:
            return {}
