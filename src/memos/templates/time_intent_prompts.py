TIME_INTENT_PROMPT_VERSION = "v1"

TIME_INTENT_PROMPT = """You turn the time wording of a memory-search query into a structured intent.

Current local time: {now} ({weekday}), time zone {timezone}.

Query: {query}

Choose one intent:
- "current": asks about the present state (now, currently, these days).
- "past": asks about an earlier state or event without naming a date (before, used to, previously, before I moved).
- "as_of": asks about a specific moment or period that can be written as dates (June 2025, last week, the end of last year).
- "recent": asks what happened lately (recently, the last few days).
- "none": the wording is not a time constraint.

For "as_of" and "recent", give "start" and "end" as ISO 8601 local dates or date-times in the time zone above; a bare date covers the whole day. Leave both null for the other intents. Never invent a date the query does not imply.

Return one JSON object and nothing else: {{"intent": "...", "start": null, "end": null, "confidence": 0.0}}"""
