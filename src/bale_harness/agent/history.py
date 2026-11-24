"""Token estimation and whole-turn history trimming.

Token counts are deterministic estimates, not provider accounting. A turn is
grouped by user messages so an assistant tool call is never separated from the
tool results it produced.
"""

import json
import math
from dataclasses import dataclass

from bale_harness.agent.messages import ChatMessage, ToolSpec

CHARS_PER_TOKEN = 3
MESSAGE_OVERHEAD_TOKENS = 4
TOOL_CALL_OVERHEAD_TOKENS = 4
TRUNCATION_MARKER = "\n[truncated]"


def estimate_tokens(text: str) -> int:
    """Estimate tokens for ``text`` using a conservative character ratio."""
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_message_tokens(message: ChatMessage) -> int:
    total = MESSAGE_OVERHEAD_TOKENS + estimate_tokens(message.content)
    total += estimate_tokens(message.name or "")
    for call in message.tool_calls:
        arguments = json.dumps(call.arguments, sort_keys=True, default=str)
        total += TOOL_CALL_OVERHEAD_TOKENS + estimate_tokens(call.name)
        total += estimate_tokens(arguments)
    return total


def estimate_tools_tokens(tools: list[ToolSpec]) -> int:
    return sum(estimate_tokens(tool.model_dump_json()) for tool in tools)


@dataclass(frozen=True)
class TrimResult:
    """Outcome of trimming a history to an available token budget."""

    messages: list[ChatMessage]
    dropped_turns: int = 0
    truncated_tools: int = 0


@dataclass
class HistoryTrimmer:
    """Drop whole turns and compress oversized tool output."""

    tool_char_limit: int = 4000

    def group(self, messages: list[ChatMessage]) -> list[list[ChatMessage]]:
        groups: list[list[ChatMessage]] = []
        current: list[ChatMessage] = []
        for message in messages:
            if message.role == "user" and current:
                groups.append(current)
                current = []
            current.append(message)
        if current:
            groups.append(current)
        return groups

    def _compact_tool_outputs(
        self, group: list[ChatMessage]
    ) -> tuple[list[ChatMessage], int]:
        compacted: list[ChatMessage] = []
        replaced = 0
        for message in group:
            if message.role == "tool" and len(message.content) > self.tool_char_limit:
                content = message.content[: self.tool_char_limit] + TRUNCATION_MARKER
                compacted.append(message.model_copy(update={"content": content}))
                replaced += 1
            else:
                compacted.append(message)
        return compacted, replaced

    def trim(
        self,
        messages: list[ChatMessage],
        available_tokens: int,
        *,
        keep_latest: bool = True,
    ) -> TrimResult:
        """Keep the newest whole turns that fit.

        ``keep_latest`` is disabled when the caller appends the current turn
        separately and previous turns must be dropped if they do not fit.
        """
        groups = self.group(messages)
        if not groups:
            return TrimResult(messages=[])

        compacted_groups: list[list[ChatMessage]] = []
        truncated = 0
        for group in groups:
            compacted, count = self._compact_tool_outputs(group)
            compacted_groups.append(compacted)
            truncated += count

        kept: list[list[ChatMessage]] = []
        used = 0
        for group in reversed(compacted_groups):
            cost = sum(estimate_message_tokens(message) for message in group)
            fits = used + cost <= available_tokens
            if not fits and (kept or not keep_latest):
                break
            kept.append(group)
            used += cost
        kept.reverse()

        trimmed = [message for group in kept for message in group]
        return TrimResult(
            messages=trimmed,
            dropped_turns=len(groups) - len(kept),
            truncated_tools=truncated,
        )

    def validate_pairing(self, messages: list[ChatMessage]) -> bool:
        """Return whether every tool message follows a matching tool call."""
        available: set[str] = set()
        for message in messages:
            if message.role == "assistant":
                available = {call.call_id for call in message.tool_calls}
            elif message.role == "tool":
                if not message.tool_call_id or message.tool_call_id not in available:
                    return False
            elif message.role == "user":
                available = set()
        return True
