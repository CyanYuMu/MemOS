REORGANIZE_PROMPT = """You are a memory clustering and summarization expert.

Given the following child memory items:

{memory_items_text}

Please perform:
1. Identify information that reflects user's experiences, beliefs, concerns, decisions, plans, or reactions — including meaningful input from assistant that user acknowledged or responded to.
2. Resolve all time, person, and event references clearly:
   - Convert relative time expressions (e.g., “yesterday,” “next Friday”) into absolute dates using the message timestamp if possible.
   - Clearly distinguish between event time and message time.
   - If uncertainty exists, state it explicitly (e.g., “around June 2025,” “exact date unclear”).
   - Include specific locations if mentioned.
   - Resolve all pronouns, aliases, and ambiguous references into full names or identities.
   - Disambiguate people with the same name if applicable.
3. Always write from a third-person perspective, referring to user as
"The user" or by name if name mentioned, rather than using first-person ("I", "me", "my").
For example, write "The user felt exhausted..." instead of "I felt exhausted...".
4. Do not omit any information that user is likely to remember.
   - Include all key experiences, thoughts, emotional responses, and plans — even if they seem minor.
   - Prioritize completeness and fidelity over conciseness.
   - Do not generalize or skip details that could be personally meaningful to user.
5. Summarize all child memory items into one memory item.

Language rules:
- The `key`, `value`, `tags`, `summary` fields must match the mostly used language of the input memory items.  **如果输入是中文，请输出中文**
- Keep `memory_type` in English.

Return valid JSON:
{
  "key": <string, a concise title of the `value` field>,
  "memory_type": <string, Either "LongTermMemory" or "UserMemory">,
  "value": <A detailed, self-contained, and unambiguous memory statement, only contain detailed, unaltered information extracted and consolidated from the input `value` fields, do not include summary content — written in English if the input memory items are in English, or in Chinese if the input is in Chinese>,
  "tags": <A list of relevant thematic keywords (e.g., ["deadline", "team", "planning"])>,
  "summary": <a natural paragraph summarizing the above memories from user's perspective, only contain information from the input `summary` fields, 120–200 words, same language as the input>
}

"""


LOCAL_SUBCLUSTER_PROMPT = """You are a memory organization expert.

You are given a cluster of memory items, each with an ID and content.
Your task is to divide these into smaller, semantically meaningful sub-clusters.

Instructions:
- Identify natural topics by analyzing common time, place, people, and event elements.
- Each sub-cluster must reflect a coherent theme that helps retrieval.
- Each sub-cluster should have 2–10 items. Discard singletons.
- Each item ID must appear in exactly one sub-cluster or be discarded. No duplicates are allowed.
- All IDs in the output must be from the provided Memory items.
- Return strictly valid JSON only.

Example: If you have items about a project across multiple phases, group them by milestone, team, or event.

Language rules:
- The `key` fields must match the mostly used language of the clustered memories. **如果输入是中文，请输出中文**

Return valid JSON:
{
  "clusters": [
    {
      "ids": ["<id1>", "<id2>", ...],
      "key": "<string, a unique, concise memory title>"
    },
    ...
  ]
}

Memory items:
{joined_scene}
"""

PAIRWISE_RELATION_PROMPT = """
You are a reasoning assistant.

Given two memory units:
- Node 1: "{node1}"
- Node 2: "{node2}"

Your task:
- Determine their relationship ONLY if it reveals NEW usable reasoning or retrieval knowledge that is NOT already explicit in either unit.
- Focus on whether combining them adds new temporal, causal, conditional, or conflict information.

Valid options:
- CAUSE: One clearly leads to the other.
- CONDITION: One happens only if the other condition holds.
- RELATE: They are semantically related by shared people, time, place, or event, but neither causes the other.
- CONFLICT: They logically contradict each other.
- NONE: No clear useful connection.

Example:
- Node 1: "The marketing campaign ended in June."
- Node 2: "Product sales dropped in July."
Answer: CAUSE

Another Example:
- Node 1: "The conference was postponed to August due to the venue being unavailable."
- Node 2: "The venue was booked for a wedding in August."
Answer: CONFLICT

Always respond with ONE word, no matter what language is for the input nodes: [CAUSE | CONDITION | RELATE | CONFLICT | NONE]
"""

INFER_FACT_PROMPT = """
You are an inference expert.

Source Memory: "{source}"
Target Memory: "{target}"

They are connected by a {relation_type} relation.
Derive ONE new factual statement that clearly combines them in a way that is NOT a trivial restatement.

Requirements:
- Include relevant time, place, people, and event details if available.
- If the inference is a logical guess, explicitly use phrases like "It can be inferred that...".

Example:
Source: "John missed the team meeting on Monday."
Target: "Important project deadlines were discussed in that meeting."
Relation: CAUSE
Inference: "It can be inferred that John may not know the new project deadlines."

If there is NO new useful fact that combines them, reply exactly: "None"
"""

AGGREGATE_PROMPT = """
You are a concept summarization assistant.

Below is a list of memory items:
{joined}

Your task:
- Identify if they can be meaningfully grouped under a new, higher-level concept that clarifies their shared time, place, people, or event context.
- Do NOT aggregate if the overlap is trivial or obvious from each unit alone.
- If the summary involves any plausible interpretation, explicitly note it (e.g., "This suggests...").

Example:
Input Memories:
- "Mary organized the 2023 sustainability summit in Berlin."
- "Mary presented a keynote on renewable energy at the same summit."

Language rules:
- The `key`, `value`, `tags`, `background` fields must match the language of the input.

Good Aggregate:
{
  "key": "Mary's Sustainability Summit Role",
  "value": "Mary organized and spoke at the 2023 sustainability summit in Berlin, highlighting renewable energy initiatives.",
  "tags": ["Mary", "summit", "Berlin", "2023"],
  "background": "Combined from multiple memories about Mary's activities at the summit."
}

If you find NO useful higher-level concept, reply exactly: "None".
"""


MEMORY_RELATION_DETECTOR_PROMPT = """You are a memory relationship analyzer.
You are given two statements about the same user. Statement A was recorded earlier than Statement B (the recording times are shown when known). Decide how B relates to A and answer with exactly one label:

supersedes: A and B describe the same changeable fact (where the user lives, works or studies; a plan, a status, a setting, an ownership) and B gives a new value that replaces A. Once B is true, A is no longer true.
redundant: A and B convey essentially the same information, even if worded differently. B adds nothing that changes the fact.
both_valid: A and B look similar or seem to disagree, but both can be true at the same time (different people, places, occasions or time points, or one is a past occasion rather than a current state).
independent: A and B are about different topics, or about complementary aspects, with no conflict.

Rules:
- A one-off event that happened at a specific time (a trip, a meeting, a purchase) never supersedes another event. Two such events are both_valid or independent.
- When unsure between supersedes and both_valid, answer both_valid.
Respond only with one of the four labels: supersedes, redundant, both_valid, independent.
Do not provide any explanation or additional text.

Statement A (recorded {time_1}): {statement_1}
Statement B (recorded {time_2}): {statement_2}
"""


MEMORY_RELATION_RESOLVER_PROMPT = """You are a memory fusion expert. You are given two statements and their associated metadata. The statements have been identified as {relation}. Your task is to analyze them carefully, considering the metadata (such as time, source, or confidence if available), and produce a single, coherent, and comprehensive statement that best represents the combined information.

If the statements are redundant, merge them by preserving all unique details and removing duplication, forming a richer, consolidated version.
If the statements are contradictory, attempt to resolve the conflict by prioritizing more recent information, higher-confidence data, or logically reconciling the differences based on context. If the contradiction is fundamental and cannot be logically resolved, output <answer>No</answer>.
Do not include any explanations, reasoning, or extra text. Only output the final result enclosed in <answer></answer> tags.
Strive to retain as much factual content as possible, especially time-specific details.
Use objective language and avoid pronouns.
Output Example 1 (unresolvable conflict):
<answer>No</answer>

Output Example 2 (successful fusion):
<answer>The meeting took place on 2023-10-05 at 14:00 in the main conference room, as confirmed by the updated schedule, and included a presentation on project milestones followed by a Q&A session.</answer>

Now, reconcile the following two statements:
Relation Type: {relation}
Statement 1: {statement_1}
Metadata 1: {metadata_1}
Statement 2: {statement_2}
Metadata 2: {metadata_2}
"""
