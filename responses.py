"""Message response helpers that do not import the AstrBot framework."""

from __future__ import annotations

from typing import Any


def plain_text_result(event: Any, text: str) -> Any:
    """Build a reply that Markdown-capable adapters must send as plain text.

    Args:
        event: AstrBot message event used to create the result.
        text: Reply text whose whitespace must be preserved.

    Returns:
        The AstrBot message result with Markdown explicitly disabled.
    """
    return event.plain_result(text).use_markdown(False)
