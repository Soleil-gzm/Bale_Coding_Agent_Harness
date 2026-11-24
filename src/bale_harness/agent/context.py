"""Minimal conversation context assembly for B1.

A full ContextBuilder with history trimming, retrieval assembly, and token
budgeting is planned for a later milestone. B1 only needs a stable system
prefix plus the current user message so the loop can run deterministically.
"""

from bale_harness.agent.messages import ChatMessage, ToolSpec
from bale_harness.contracts import Turn

SYSTEM_PROMPT = (
    "You are Bale, a coding agent. Use the provided read-only tools to inspect "
    "the workspace. When you have enough information, reply with a final answer "
    "and no tool calls."
)


def build_initial_messages(turn: Turn, tools: list[ToolSpec]) -> list[ChatMessage]:
    """Build the starting message list: system prompt + current user message."""
    parts = [SYSTEM_PROMPT]
    if tools:
        descriptions = "\n".join(f"- {tool.name}: {tool.description}" for tool in tools)
        parts.append("Available tools:\n" + descriptions)
    system_content = "\n\n".join(parts)
    return [
        ChatMessage(role="system", content=system_content),
        ChatMessage(role="user", content=turn.message),
    ]
