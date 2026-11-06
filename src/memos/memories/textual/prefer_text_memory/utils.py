import json

from memos.types import MessageList


def convert_messages_to_string(messages: MessageList) -> str:
    """Convert a list of messages to a string."""
    message_text = ""
    for message in messages:
        content = message.get("content", "")
        content = (
            content.strip()
            if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False).strip()
        )
        if message["role"] == "system":
            continue
        if message["role"] == "user":
            message_text += f"User: {content}\n" if content else ""
        elif message["role"] == "assistant":
            tool_calls = message.get("tool_calls", [])
            tool_calls_str = (
                f"[tool_calls]: {json.dumps(tool_calls, ensure_ascii=False)}" if tool_calls else ""
            )
            line_str = (
                f"Assistant: {content} {tool_calls_str}".strip()
                if content or tool_calls_str
                else ""
            )
            message_text += f"{line_str}\n" if line_str else ""
        elif message["role"] == "tool":
            tool_call_id = message.get("tool_call_id", "")
            line_str = (
                f"Tool: {content} [tool_call_id]: {tool_call_id}".strip()
                if tool_call_id
                else f"Tool: {content}".strip()
            )
            message_text += f"{line_str}\n" if line_str else ""
    return message_text.strip()
