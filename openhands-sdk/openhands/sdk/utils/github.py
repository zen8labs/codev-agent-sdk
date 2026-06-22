"""Utility functions for GitHub integrations."""

import re


# Zero-width joiner character (U+200D)
# We use ZWJ instead of ZWSP (U+200B) because:
# - ZWJ is semantically more appropriate (joins characters without adding space)
# - ZWJ has better support in modern renderers
# - ZWJ is invisible and doesn't affect text rendering or selection
ZWJ = "\u200d"


def sanitize_agent_mentions(text: str) -> str:
    """Sanitize @z8l-agent mentions in text to prevent self-mention loops.

    This function inserts a zero-width joiner (ZWJ) after the @ symbol in
    @z8l-agent mentions, making them non-clickable in GitHub comments while
    preserving readability. The original case of the mention is preserved.

    Args:
        text: The text to sanitize

    Returns:
        Text with sanitized @z8l-agent mentions (e.g., "@z8l-agent" -> "@‍z8l-agent")

    Examples:
        >>> sanitize_agent_mentions("Thanks @z8l-agent for the help!")
        'Thanks @\\u200dz8l-agent for the help!'
        >>> sanitize_agent_mentions("Check @z8l-agent and @Z8L-AGENT")
        'Check @\\u200dz8l-agent and @\\u200dZ8L-AGENT'
        >>> sanitize_agent_mentions("No mention here")
        'No mention here'
    """
    # Pattern to match @z8l-agent mentions at word boundaries
    # Uses re.IGNORECASE so we don't need [Zz]8[Ll]-[Aa]gent
    # Capture group preserves the original case
    pattern = r"@(z8l-agent)\b"

    # Replace @ with @ + ZWJ while preserving the original case
    # The \1 backreference preserves the matched case
    sanitized = re.sub(pattern, f"@{ZWJ}\\1", text, flags=re.IGNORECASE)

    return sanitized
