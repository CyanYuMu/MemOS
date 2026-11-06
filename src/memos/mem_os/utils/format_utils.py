import random

from typing import Any

from memos.log import get_logger


logger = get_logger(__name__)


def extract_node_name(memory: str) -> str:
    """Extract the first two words from memory as node_name"""
    if not memory:
        return ""

    words = [word.strip() for word in memory.split() if word.strip()]

    if len(words) >= 2:
        return " ".join(words[:2])
    elif len(words) == 1:
        return words[0]
    else:
        return ""


def analyze_tree_structure_enhanced(nodes: list[dict], edges: list[dict]) -> dict:
    """Enhanced tree structure analysis, focusing on branching degree and leaf distribution"""
    # Build adjacency list
    adj_list = {}
    reverse_adj = {}
    for edge in edges:
        source, target = edge["source"], edge["target"]
        adj_list.setdefault(source, []).append(target)
        reverse_adj.setdefault(target, []).append(source)

    # Find all nodes and root nodes
    all_nodes = {node["id"] for node in nodes}
    target_nodes = {edge["target"] for edge in edges}
    root_nodes = all_nodes - target_nodes

    subtree_analysis = {}

    def analyze_subtree_enhanced(root_id: str) -> dict:
        """Enhanced subtree analysis, focusing on branching degree and structure quality"""
        visited = set()
        max_depth = 0
        leaf_count = 0
        total_nodes = 0
        branch_nodes = 0  # Number of branch nodes with multiple children
        chain_length = 0  # Longest single chain length
        width_per_level = {}  # Width per level

        def dfs(node_id: str, depth: int, chain_len: int):
            nonlocal max_depth, leaf_count, total_nodes, branch_nodes, chain_length

            if node_id in visited:
                return

            visited.add(node_id)
            total_nodes += 1
            max_depth = max(max_depth, depth)
            chain_length = max(chain_length, chain_len)

            # Record number of nodes per level
            width_per_level[depth] = width_per_level.get(depth, 0) + 1

            children = adj_list.get(node_id, [])

            if not children:  # Leaf node
                leaf_count += 1
            elif len(children) > 1:  # Branch node
                branch_nodes += 1
                # Reset chain length because we encountered a branch
                for child in children:
                    dfs(child, depth + 1, 0)
            else:  # Single child node (chain structure)
                for child in children:
                    dfs(child, depth + 1, chain_len + 1)

        dfs(root_id, 0, 0)

        # Calculate structure quality metrics
        avg_width = sum(width_per_level.values()) / len(width_per_level) if width_per_level else 0
        max_width = max(width_per_level.values()) if width_per_level else 0

        # Calculate branch density: ratio of branch nodes to total nodes
        branch_density = branch_nodes / total_nodes if total_nodes > 0 else 0

        # Calculate depth-width ratio: ideal tree should have moderate depth and good breadth
        depth_width_ratio = max_depth / max_width if max_width > 0 else max_depth

        quality_score = calculate_enhanced_quality(
            max_depth,
            leaf_count,
            total_nodes,
            branch_nodes,
            chain_length,
            branch_density,
            depth_width_ratio,
            max_width,
        )

        return {
            "root_id": root_id,
            "max_depth": max_depth,
            "leaf_count": leaf_count,
            "total_nodes": total_nodes,
            "branch_nodes": branch_nodes,
            "max_chain_length": chain_length,
            "branch_density": branch_density,
            "max_width": max_width,
            "avg_width": avg_width,
            "depth_width_ratio": depth_width_ratio,
            "nodes_in_subtree": list(visited),
            "quality_score": quality_score,
            "width_per_level": width_per_level,
        }

    for root_id in root_nodes:
        subtree_analysis[root_id] = analyze_subtree_enhanced(root_id)

    return subtree_analysis


def calculate_enhanced_quality(
    max_depth: int,
    leaf_count: int,
    total_nodes: int,
    branch_nodes: int,
    max_chain_length: int,
    branch_density: float,
    depth_width_ratio: float,
    max_width: int,
) -> float:
    """Enhanced quality calculation, prioritizing branching degree and leaf distribution"""

    if total_nodes <= 1:
        return 0.1

    # 1. Branch quality score (weight: 35%)
    # Branch node count score
    branch_count_score = min(branch_nodes * 3, 15)  # 3 points per branch node, max 15 points

    # Branch density score: ideal density between 20%-60%
    if 0.2 <= branch_density <= 0.6:
        branch_density_score = 10
    elif branch_density > 0.6:
        branch_density_score = max(5, 10 - (branch_density - 0.6) * 20)
    else:
        branch_density_score = branch_density * 25  # Linear growth for 0-20%

    branch_score = (branch_count_score + branch_density_score) * 0.35

    # 2. Leaf quality score (weight: 25%)
    # Leaf count score
    leaf_count_score = min(leaf_count * 2, 20)

    # Leaf distribution score: ideal leaf ratio 30%-70% of total nodes
    leaf_ratio = leaf_count / total_nodes
    if 0.3 <= leaf_ratio <= 0.7:
        leaf_ratio_score = 10
    elif leaf_ratio > 0.7:
        leaf_ratio_score = max(3, 10 - (leaf_ratio - 0.7) * 20)
    else:
        leaf_ratio_score = leaf_ratio * 20  # Linear growth for 0-30%

    leaf_score = (leaf_count_score + leaf_ratio_score) * 0.25

    # 3. Structure balance score (weight: 25%)
    # Depth score: moderate depth is best (3-8 layers)
    if 3 <= max_depth <= 8:
        depth_score = 15
    elif max_depth < 3:
        depth_score = max_depth * 3  # Lower score for 1-2 layers
    else:
        depth_score = max(5, 15 - (max_depth - 8) * 1.5)  # Gradually reduce score beyond 8 layers

    # Width score: larger max width is better, but with upper limit
    width_score = min(max_width * 1.5, 15)

    # Depth-width ratio penalty: too large ratio means tree is too "thin"
    if depth_width_ratio > 3:
        ratio_penalty = (depth_width_ratio - 3) * 2
        structure_score = max(0, (depth_score + width_score - ratio_penalty)) * 0.25
    else:
        structure_score = (depth_score + width_score) * 0.25

    # 4. Chain structure penalty (weight: 15%)
    # Longest single chain length penalty: overly long chains severely affect display
    if max_chain_length <= 2:
        chain_penalty_score = 10
    elif max_chain_length <= 5:
        chain_penalty_score = 8 - (max_chain_length - 2)
    else:
        chain_penalty_score = max(0, 3 - (max_chain_length - 5) * 0.5)

    chain_score = chain_penalty_score * 0.15

    # 5. Comprehensive calculation
    total_score = branch_score + leaf_score + structure_score + chain_score

    # Special case severe penalties
    if max_chain_length > total_nodes * 0.8:  # If more than 80% are single chains
        total_score *= 0.3
    elif branch_density < 0.1 and total_nodes > 5:  # Large tree with almost no branches
        total_score *= 0.5

    return total_score


def sample_nodes_with_type_balance(
    nodes: list[dict],
    edges: list[dict],
    target_count: int = 150,
    type_ratios: dict[str, float] | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Balanced sampling based on type ratios and tree quality

    Args:
        nodes: List of nodes
        edges: List of edges
        target_count: Target number of nodes
        type_ratios: Expected ratio for each type, e.g. {'WorkingMemory': 0.15, 'EpisodicMemory': 0.30, ...}
    """
    if len(nodes) <= target_count:
        return nodes, edges

    # Default type ratio configuration
    if type_ratios is None:
        type_ratios = {
            "WorkingMemory": 0.10,  # 10%
            "EpisodicMemory": 0.25,  # 25%
            "SemanticMemory": 0.25,  # 25%
            "ProceduralMemory": 0.20,  # 20%
            "EmotionalMemory": 0.15,  # 15%
            "MetaMemory": 0.05,  # 5%
        }

    logger.info(
        f"Starting type-balanced sampling, original nodes: {len(nodes)}, target nodes: {target_count}"
    )
    logger.info(f"Target type ratios: {type_ratios}")

    # Analyze current node type distribution
    current_type_counts = {}
    nodes_by_type = {}

    for node in nodes:
        memory_type = node.get("metadata", {}).get("memory_type", "Unknown")
        current_type_counts[memory_type] = current_type_counts.get(memory_type, 0) + 1
        if memory_type not in nodes_by_type:
            nodes_by_type[memory_type] = []
        nodes_by_type[memory_type].append(node)

    logger.info(f"Current type distribution: {current_type_counts}")

    # Calculate target node count for each type
    type_targets = {}
    remaining_target = target_count

    for memory_type, ratio in type_ratios.items():
        if memory_type in nodes_by_type:
            target_for_type = int(target_count * ratio)
            # Ensure not exceeding the actual node count for this type
            target_for_type = min(target_for_type, len(nodes_by_type[memory_type]))
            type_targets[memory_type] = target_for_type
            remaining_target -= target_for_type

    # Handle types not in ratio configuration
    other_types = set(nodes_by_type.keys()) - set(type_ratios.keys())
    if other_types and remaining_target > 0:
        per_other_type = max(1, remaining_target // len(other_types))
        for memory_type in other_types:
            allocation = min(per_other_type, len(nodes_by_type[memory_type]))
            type_targets[memory_type] = allocation
            remaining_target -= allocation

    # If there's still remaining, distribute proportionally to main types
    if remaining_target > 0:
        main_types = [t for t in type_ratios if t in nodes_by_type]
        if main_types:
            extra_per_type = remaining_target // len(main_types)
            for memory_type in main_types:
                additional = min(
                    extra_per_type,
                    len(nodes_by_type[memory_type]) - type_targets.get(memory_type, 0),
                )
                type_targets[memory_type] = type_targets.get(memory_type, 0) + additional

    logger.info(f"Target node count for each type: {type_targets}")

    # Perform subtree quality sampling for each type
    selected_nodes = []

    for memory_type, target_for_type in type_targets.items():
        if target_for_type <= 0 or memory_type not in nodes_by_type:
            continue

        type_nodes = nodes_by_type[memory_type]
        logger.info(
            f"\n--- Processing {memory_type} type: {len(type_nodes)} -> {target_for_type} ---"
        )

        if len(type_nodes) <= target_for_type:
            selected_nodes.extend(type_nodes)
            logger.info(f"  Select all: {len(type_nodes)} nodes")
        else:
            # Use enhanced subtree quality sampling
            type_selected = sample_by_enhanced_subtree_quality(type_nodes, edges, target_for_type)
            selected_nodes.extend(type_selected)
            logger.info(f"  Sampled selection: {len(type_selected)} nodes")

    # Filter edges
    selected_node_ids = {node["id"] for node in selected_nodes}
    filtered_edges = [
        edge
        for edge in edges
        if edge["source"] in selected_node_ids and edge["target"] in selected_node_ids
    ]

    logger.info(f"\nFinal selected nodes: {len(selected_nodes)}")
    logger.info(f"Final edges: {len(filtered_edges)}")

    # Verify final type distribution
    final_type_counts = {}
    for node in selected_nodes:
        memory_type = node.get("metadata", {}).get("memory_type", "Unknown")
        final_type_counts[memory_type] = final_type_counts.get(memory_type, 0) + 1

    logger.info(f"Final type distribution: {final_type_counts}")
    for memory_type, count in final_type_counts.items():
        percentage = count / len(selected_nodes) * 100
        target_percentage = type_ratios.get(memory_type, 0) * 100
        logger.info(
            f"  {memory_type}: {count} nodes ({percentage:.1f}%, target: {target_percentage:.1f}%)"
        )

    return selected_nodes, filtered_edges


def sample_by_enhanced_subtree_quality(
    nodes: list[dict], edges: list[dict], target_count: int
) -> list[dict]:
    """Sample using enhanced subtree quality"""
    if len(nodes) <= target_count:
        return nodes

    # Analyze subtree structure
    subtree_analysis = analyze_tree_structure_enhanced(nodes, edges)

    if not subtree_analysis:
        # If no subtree structure, sample by node importance
        return sample_nodes_by_importance(nodes, edges, target_count)

    # Sort subtrees by quality score
    sorted_subtrees = sorted(
        subtree_analysis.items(), key=lambda x: x[1]["quality_score"], reverse=True
    )

    logger.info("  Subtree quality ranking:")
    for i, (root_id, analysis) in enumerate(sorted_subtrees[:5]):
        logger.info(
            f"    #{i + 1} Root node {root_id}: Quality={analysis['quality_score']:.2f}, "
            f"Depth={analysis['max_depth']}, Branches={analysis['branch_nodes']}, "
            f"Leaves={analysis['leaf_count']}, Max Width={analysis['max_width']}"
        )

    # Greedy selection of high-quality subtrees
    selected_nodes = []
    selected_node_ids = set()

    for root_id, analysis in sorted_subtrees:
        subtree_nodes = analysis["nodes_in_subtree"]
        new_nodes = [node_id for node_id in subtree_nodes if node_id not in selected_node_ids]

        if not new_nodes:
            continue

        remaining_quota = target_count - len(selected_nodes)

        if len(new_nodes) <= remaining_quota:
            # Entire subtree can be added
            for node_id in new_nodes:
                node = next((n for n in nodes if n["id"] == node_id), None)
                if node:
                    selected_nodes.append(node)
                    selected_node_ids.add(node_id)
            logger.info(f"    Select entire subtree {root_id}: +{len(new_nodes)} nodes")
        else:
            # Subtree too large, need partial selection
            if analysis["quality_score"] > 5:  # Only partial selection for high-quality subtrees
                subtree_node_objects = [n for n in nodes if n["id"] in new_nodes]
                partial_selection = select_best_nodes_from_subtree(
                    subtree_node_objects, edges, remaining_quota, root_id
                )

                selected_nodes.extend(partial_selection)
                for node in partial_selection:
                    selected_node_ids.add(node["id"])
                logger.info(
                    f"    Partial selection of subtree {root_id}: +{len(partial_selection)} nodes"
                )

        if len(selected_nodes) >= target_count:
            break

    # If target count not reached, supplement with remaining nodes
    if len(selected_nodes) < target_count:
        remaining_nodes = [n for n in nodes if n["id"] not in selected_node_ids]
        remaining_count = target_count - len(selected_nodes)
        additional = sample_nodes_by_importance(remaining_nodes, edges, remaining_count)
        selected_nodes.extend(additional)
        logger.info(f"    Supplementary selection: +{len(additional)} nodes")

    return selected_nodes


def select_best_nodes_from_subtree(
    subtree_nodes: list[dict], edges: list[dict], max_count: int, root_id: str
) -> list[dict]:
    """Select the most important nodes from subtree, prioritizing branch structure"""
    if len(subtree_nodes) <= max_count:
        return subtree_nodes

    # Build internal connection relationships of subtree
    subtree_node_ids = {node["id"] for node in subtree_nodes}
    subtree_edges = [
        edge
        for edge in edges
        if edge["source"] in subtree_node_ids and edge["target"] in subtree_node_ids
    ]

    # Calculate importance score for each node
    node_scores = []

    for node in subtree_nodes:
        node_id = node["id"]

        # Out-degree and in-degree
        out_degree = sum(1 for edge in subtree_edges if edge["source"] == node_id)
        in_degree = sum(1 for edge in subtree_edges if edge["target"] == node_id)

        # Content length score
        content_score = min(len(node.get("memory", "")), 300) / 15

        # Branch node bonus
        branch_bonus = out_degree * 8 if out_degree > 1 else 0

        # Root node bonus
        root_bonus = 15 if node_id == root_id else 0

        # Connection importance
        connection_score = (out_degree + in_degree) * 3

        # Leaf node moderate bonus (ensure certain number of leaf nodes)
        leaf_bonus = 5 if out_degree == 0 and in_degree > 0 else 0

        total_score = content_score + connection_score + branch_bonus + root_bonus + leaf_bonus
        node_scores.append((node, total_score))

    # Sort by score and select
    node_scores.sort(key=lambda x: x[1], reverse=True)
    selected = [node for node, _ in node_scores[:max_count]]

    return selected


def sample_nodes_by_importance(
    nodes: list[dict], edges: list[dict], target_count: int
) -> list[dict]:
    """Sample by node importance (for cases without tree structure)"""
    if len(nodes) <= target_count:
        return nodes

    node_scores = []

    for node in nodes:
        node_id = node["id"]
        out_degree = sum(1 for edge in edges if edge["source"] == node_id)
        in_degree = sum(1 for edge in edges if edge["target"] == node_id)
        content_score = min(len(node.get("memory", "")), 200) / 10
        connection_score = (out_degree + in_degree) * 5
        random_score = random.random() * 10

        total_score = content_score + connection_score + random_score
        node_scores.append((node, total_score))

    node_scores.sort(key=lambda x: x[1], reverse=True)
    return [node for node, _ in node_scores[:target_count]]


# Modified main function to use new sampling strategy


def convert_graph_to_tree_forworkmem(
    json_data: dict[str, Any],
    target_node_count: int = 200,
    type_ratios: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Enhanced graph-to-tree conversion function, prioritizing branching degree and type balance
    """
    original_nodes = json_data.get("nodes", [])
    original_edges = json_data.get("edges", [])

    logger.info(f"Original node count: {len(original_nodes)}")
    logger.info(f"Target node count: {target_node_count}")
    filter_original_edges = []
    for original_edge in original_edges:
        if original_edge["type"] == "PARENT":
            filter_original_edges.append(original_edge)
    node_type_count = {}
    for node in original_nodes:
        node_type = node.get("metadata", {}).get("memory_type", "Unknown")
        node_type_count[node_type] = node_type_count.get(node_type, 0) + 1
    original_edges = filter_original_edges
    # Use enhanced type-balanced sampling
    if len(original_nodes) > target_node_count:
        nodes, edges = sample_nodes_with_type_balance(
            original_nodes, original_edges, target_node_count, type_ratios
        )
    else:
        nodes, edges = original_nodes, original_edges

    # The rest of tree structure building remains unchanged...
    # [Original tree building code here]

    # Create node mapping table
    node_map = {}
    for node in nodes:
        memory = node.get("memory", "")
        node_name = extract_node_name(memory)
        memory_key = node.get("metadata", {}).get("key", node_name)
        usage = node.get("metadata", {}).get("usage", [])
        frequency = len(usage) if len(usage) < 100 else 100
        node_map[node["id"]] = {
            "id": node["id"],
            "value": memory,
            "frequency": frequency,
            "node_name": memory_key,
            "memory_type": node.get("metadata", {}).get("memory_type", "Unknown"),
            "children": [],
        }

    # Build parent-child relationship mapping
    children_map = {}
    parent_map = {}

    for edge in edges:
        source = edge["source"]
        target = edge["target"]
        if source not in children_map:
            children_map[source] = []
        children_map[source].append(target)
        parent_map[target] = source

    # Find root nodes
    all_node_ids = set(node_map.keys())
    children_node_ids = set(parent_map.keys())
    root_node_ids = all_node_ids - children_node_ids

    # Separate WorkingMemory and other root nodes
    working_memory_roots = []
    other_roots = []

    for root_id in root_node_ids:
        if node_map[root_id]["memory_type"] == "WorkingMemory":
            working_memory_roots.append(root_id)
        else:
            other_roots.append(root_id)

    def build_tree(node_id: str, visited=None) -> dict[str, Any] | None:
        """Recursively build tree structure with cycle detection"""
        if visited is None:
            visited = set()

        if node_id in visited:
            logger.warning(f"[build_tree] Detected cycle at node {node_id}, skipping.")
            return None
        visited.add(node_id)

        if node_id not in node_map:
            return None

        children_ids = children_map.get(node_id, [])
        children = []
        for child_id in children_ids:
            child_tree = build_tree(child_id, visited)
            if child_tree:
                children.append(child_tree)

        node = {
            "id": node_id,
            "node_name": node_map[node_id]["node_name"],
            "value": node_map[node_id]["value"],
            "memory_type": node_map[node_id]["memory_type"],
            "frequency": node_map[node_id]["frequency"],
        }

        if children:
            node["children"] = children

        return node

    # Build root tree list
    root_trees = []
    for root_id in other_roots:
        tree = build_tree(root_id)
        if tree:
            root_trees.append(tree)

    # Handle WorkingMemory
    if working_memory_roots:
        working_memory_children = []
        for wm_root_id in working_memory_roots:
            tree = build_tree(wm_root_id)
            if tree:
                working_memory_children.append(tree)

        working_memory_node = {
            "id": "WorkingMemory",
            "node_name": "WorkingMemory",
            "value": "WorkingMemory",
            "memory_type": "WorkingMemory",
            "children": working_memory_children,
            "frequency": 0,
        }

        root_trees.append(working_memory_node)

    # Create total root node
    result = {
        "id": "root",
        "node_name": "root",
        "value": "root",
        "memory_type": "Root",
        "children": root_trees,
        "frequency": 0,
    }

    return result, node_type_count


def print_tree_structure(node: dict[str, Any], level: int = 0, max_level: int = 5):
    """logger.info the first few layers of tree structure for easy viewing"""
    if level > max_level:
        return

    indent = "  " * level
    node_id = node.get("id", "unknown")
    node_name = node.get("node_name", "")
    node_value = node.get("value", "")
    memory_type = node.get("memory_type", "Unknown")

    # Determine display method based on whether there are children
    children = node.get("children", [])
    if children:
        # Intermediate node, display name, type and child count
        logger.info(f"{indent}- {node_name} [{memory_type}] ({len(children)} children)")
        logger.info(f"{indent}  ID: {node_id}")
        display_value = node_value[:80] + "..." if len(node_value) > 80 else node_value
        logger.info(f"{indent}  Value: {display_value}")

        if level < max_level:
            for child in children:
                print_tree_structure(child, level + 1, max_level)
        elif level == max_level:
            logger.info(f"{indent}  ... (expansion limited)")
    else:
        # Leaf node, display name, type and value
        display_value = node_value[:80] + "..." if len(node_value) > 80 else node_value
        logger.info(f"{indent}- {node_name} [{memory_type}]: {display_value}")
        logger.info(f"{indent}  ID: {node_id}")


def remove_embedding_recursive(memory_info: dict) -> Any:
    """remove the embedding from the memory info
    Args:
        memory_info: product memory info

    Returns:
        Any: product memory info without embedding
    """
    if isinstance(memory_info, dict):
        new_dict = {}
        for key, value in memory_info.items():
            if key != "embedding":
                new_dict[key] = remove_embedding_recursive(value)
        return new_dict
    elif isinstance(memory_info, list):
        return [remove_embedding_recursive(item) for item in memory_info]
    else:
        return memory_info


def sort_children_by_memory_type(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    sort the children by the memory_type
    Args:
        children: the children of the node
    Returns:
        the sorted children
    """
    if not children:
        return children

    def get_sort_key(child):
        memory_type = child.get("memory_type", "Unknown")
        # Sort directly by memory_type string, same types will naturally cluster together
        return memory_type

    # Sort by memory_type
    sorted_children = sorted(children, key=get_sort_key)

    return sorted_children


def extract_all_ids_from_tree(tree_node):
    """
    Recursively traverse tree structure to extract all node IDs

    Args:
        tree_node: Tree node (dictionary format)

    Returns:
        set: Set containing all node IDs
    """
    ids = set()

    # Add current node ID (if exists)
    if "id" in tree_node:
        ids.add(tree_node["id"])

    # Recursively process child nodes
    if tree_node.get("children"):
        for child in tree_node["children"]:
            ids.update(extract_all_ids_from_tree(child))

    return ids


def filter_nodes_by_tree_ids(tree_data, nodes_data):
    """
    Filter nodes list based on IDs used in tree structure

    Args:
        tree_data: Tree structure data (dictionary)
        nodes_data: Data containing nodes list (dictionary)

    Returns:
        dict: Filtered nodes data, maintaining original structure
    """
    # Extract all IDs used in the tree
    used_ids = extract_all_ids_from_tree(tree_data)

    # Filter nodes list, keeping only nodes with IDs used in the tree
    filtered_nodes = [node for node in nodes_data["nodes"] if node["id"] in used_ids]

    # Return result maintaining original structure
    return {"nodes": filtered_nodes}


def detect_and_remove_duplicate_ids(tree_node: dict[str, Any]) -> dict[str, Any]:
    """
    Detect and remove duplicate IDs in tree structure by skipping duplicate nodes.
    First occurrence of each ID is kept, subsequent duplicates are removed.

    Args:
        tree_node: Tree node (dictionary format)

    Returns:
        dict: Fixed tree node with duplicate nodes removed
    """
    used_ids = set()
    removed_count = 0

    def remove_duplicates_recursive(
        node: dict[str, Any], parent_path: str = ""
    ) -> dict[str, Any] | None:
        """Recursively remove duplicate IDs by skipping duplicate nodes"""
        nonlocal removed_count

        if not isinstance(node, dict):
            return node

        # Create node copy
        fixed_node = node.copy()

        # Handle current node ID
        current_id = fixed_node.get("id", "")
        if current_id in used_ids and current_id not in ["root", "WorkingMemory"]:
            # Skip this duplicate node
            logger.info(f"Skipping duplicate node: {current_id} (path: {parent_path})")
            removed_count += 1
            return None  # Return None to indicate this node should be removed
        else:
            used_ids.add(current_id)

        # Recursively process child nodes
        if "children" in fixed_node and isinstance(fixed_node["children"], list):
            fixed_children = []
            for i, child in enumerate(fixed_node["children"]):
                child_path = f"{parent_path}/{fixed_node.get('node_name', 'unknown')}[{i}]"
                fixed_child = remove_duplicates_recursive(child, child_path)
                if fixed_child is not None:  # Only add non-None children
                    fixed_children.append(fixed_child)
            fixed_node["children"] = fixed_children

        return fixed_node

    result = remove_duplicates_recursive(tree_node)
    if result is not None:
        logger.info(f"Removed {removed_count} duplicate nodes")
        return result
    else:
        # If root node itself was removed (shouldn't happen), return empty root
        return {
            "id": "root",
            "node_name": "root",
            "value": "root",
            "memory_type": "Root",
            "children": [],
        }


def validate_tree_structure(tree_node: dict[str, Any]) -> dict[str, Any]:
    """
    Validate tree structure integrity, including ID uniqueness check

    Args:
        tree_node: Tree node (dictionary format)

    Returns:
        dict: Validation result containing error messages and fix suggestions
    """
    validation_result = {
        "is_valid": True,
        "errors": [],
        "warnings": [],
        "total_nodes": 0,
        "unique_ids": set(),
        "duplicate_ids": set(),
        "missing_ids": set(),
        "invalid_structure": [],
    }

    def validate_recursive(node: dict[str, Any], path: str = "", depth: int = 0):
        """Recursively validate tree structure"""
        if not isinstance(node, dict):
            validation_result["errors"].append(f"Node is not a dictionary: {path}")
            validation_result["is_valid"] = False
            return

        validation_result["total_nodes"] += 1

        # Check required fields
        if "id" not in node:
            validation_result["errors"].append(f"Node missing ID field: {path}")
            validation_result["missing_ids"].add(path)
            validation_result["is_valid"] = False
        else:
            node_id = node["id"]
            if node_id in validation_result["unique_ids"]:
                validation_result["errors"].append(f"Duplicate node ID: {node_id} (path: {path})")
                validation_result["duplicate_ids"].add(node_id)
                validation_result["is_valid"] = False
            else:
                validation_result["unique_ids"].add(node_id)

        # Check other required fields
        required_fields = ["node_name", "value", "memory_type"]
        for field in required_fields:
            if field not in node:
                validation_result["warnings"].append(f"Node missing field '{field}': {path}")

        # Recursively validate child nodes
        if "children" in node:
            if not isinstance(node["children"], list):
                validation_result["errors"].append(f"Children field is not a list: {path}")
                validation_result["is_valid"] = False
            else:
                for i, child in enumerate(node["children"]):
                    child_path = f"{path}/children[{i}]"
                    validate_recursive(child, child_path, depth + 1)

        # Check depth limit
        if depth > 20:
            validation_result["warnings"].append(f"Tree depth too deep ({depth}): {path}")

    validate_recursive(tree_node)

    # Generate fix suggestions
    if validation_result["duplicate_ids"]:
        validation_result["fix_suggestion"] = (
            "Use detect_and_fix_duplicate_ids() function to fix duplicate IDs"
        )

    return validation_result


def ensure_unique_tree_ids(tree_result: dict[str, Any]) -> dict[str, Any]:
    """
    Ensure all node IDs in tree structure are unique by removing duplicate nodes,
    this is a post-processing function for convert_graph_to_tree_forworkmem

    Args:
        tree_result: Tree structure returned by convert_graph_to_tree_forworkmem

    Returns:
        dict: Fixed tree structure with duplicate nodes removed
    """
    logger.info("🔍 Starting duplicate ID check in tree structure...")

    # First validate tree structure
    validation = validate_tree_structure(tree_result)

    if validation["is_valid"]:
        logger.info("Tree structure validation passed, no duplicate IDs found")
        return tree_result

    # Report issues
    logger.info(f"Found {len(validation['errors'])} errors:")
    for error in validation["errors"][:5]:  # Only show first 5 errors
        logger.info(f"   - {error}")

    if len(validation["errors"]) > 5:
        logger.info(f"   ... and {len(validation['errors']) - 5} more errors")

    logger.info("Statistics:")
    logger.info(f"   - Total nodes: {validation['total_nodes']}")
    logger.info(f"   - Unique IDs: {len(validation['unique_ids'])}")
    logger.info(f"   - Duplicate IDs: {len(validation['duplicate_ids'])}")

    # Remove duplicate nodes
    logger.info(" Starting duplicate node removal...")
    fixed_tree = detect_and_remove_duplicate_ids(tree_result)

    # Validate again
    post_validation = validate_tree_structure(fixed_tree)
    if post_validation["is_valid"]:
        logger.info("Removal completed, tree structure is now valid")
        logger.info(f"Final node count: {post_validation['total_nodes']}")
    else:
        logger.info("Issues remain after removal, please check code logic")
        for error in post_validation["errors"][:3]:
            logger.info(f"   - {error}")

    return fixed_tree


def clean_json_response(response: str) -> str:
    """
    Remove markdown JSON code block formatting from LLM response.

    Args:
        response: Raw response string that may contain ```json and ```

    Returns:
        str: Clean JSON string without markdown formatting

    Raises:
        ValueError: If ``response`` is None. This is almost always an upstream
            failure (LLM call returned None instead of raising) and surfacing
            it here is much easier to diagnose than the implicit
            ``AttributeError: 'NoneType' object has no attribute 'replace'``
            that would otherwise be thrown.
    """
    if response is None:
        raise ValueError(
            "clean_json_response received None — upstream LLM call likely "
            "failed silently (check timed_with_status / generate() error "
            "handling)."
        )
    return response.replace("```json", "").replace("```", "").strip()
