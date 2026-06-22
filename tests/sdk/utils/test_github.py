"""Tests for GitHub utility functions."""

from openhands.sdk.utils.github import ZWJ, sanitize_agent_mentions


def test_sanitize_basic_mention():
    """Test basic @z8l-agent mention is sanitized."""
    text = "Thanks @z8l-agent for the help!"
    expected = f"Thanks @{ZWJ}z8l-agent for the help!"
    assert sanitize_agent_mentions(text) == expected


def test_sanitize_case_insensitive():
    """Test that mentions are sanitized regardless of case."""
    test_cases = [
        ("Check @z8l-agent here", f"Check @{ZWJ}z8l-agent here"),
        ("Check @Z8L-AGENT here", f"Check @{ZWJ}Z8L-AGENT here"),
        ("Check @Z8l-agent here", f"Check @{ZWJ}Z8l-agent here"),
    ]
    for input_text, expected in test_cases:
        assert sanitize_agent_mentions(input_text) == expected


def test_sanitize_multiple_mentions():
    """Test multiple mentions in the same text."""
    text = "Both @z8l-agent and @Z8L-AGENT should be sanitized"
    expected = f"Both @{ZWJ}z8l-agent and @{ZWJ}Z8L-AGENT should be sanitized"
    assert sanitize_agent_mentions(text) == expected


def test_sanitize_with_punctuation():
    """Test mentions followed by punctuation."""
    test_cases = [
        ("Thanks @z8l-agent!", f"Thanks @{ZWJ}z8l-agent!"),
        ("Hello @z8l-agent.", f"Hello @{ZWJ}z8l-agent."),
        ("See @z8l-agent,", f"See @{ZWJ}z8l-agent,"),
        ("By @z8l-agent:", f"By @{ZWJ}z8l-agent:"),
        ("From @z8l-agent;", f"From @{ZWJ}z8l-agent;"),
        ("Hi @z8l-agent?", f"Hi @{ZWJ}z8l-agent?"),
        ("Use @z8l-agent)", f"Use @{ZWJ}z8l-agent)"),
        ("Try (@z8l-agent)", f"Try (@{ZWJ}z8l-agent)"),
    ]
    for input_text, expected in test_cases:
        assert sanitize_agent_mentions(input_text) == expected


def test_no_sanitize_partial_words():
    """Test that partial word matches are NOT sanitized."""
    test_cases = [
        "z8l-agentTeam",
        "Myz8l-agent",
        "z8l-agentBot",
        "#z8l-agent",
    ]
    for text in test_cases:
        # Partial words without @ should remain unchanged
        assert sanitize_agent_mentions(text) == text


def test_no_op_cases():
    """Test cases where no sanitization should occur."""
    test_cases = [
        "",
        "No mentions here",
        "Just some text",
        "@GitHub",
        "@Other",
        "z8l-agent without @",
    ]
    for text in test_cases:
        assert sanitize_agent_mentions(text) == text


def test_sanitize_at_line_boundaries():
    """Test mentions at the start and end of lines."""
    test_cases = [
        ("@z8l-agent at start", f"@{ZWJ}z8l-agent at start"),
        ("at end @z8l-agent", f"at end @{ZWJ}z8l-agent"),
        ("@z8l-agent", f"@{ZWJ}z8l-agent"),
    ]
    for input_text, expected in test_cases:
        assert sanitize_agent_mentions(input_text) == expected


def test_sanitize_multiline_text():
    """Test sanitization in multiline text."""
    text = """Hello @z8l-agent!

This is a test with @Z8L-AGENT mentioned.

Thanks @Z8l-agent for everything!"""

    expected = f"""Hello @{ZWJ}z8l-agent!

This is a test with @{ZWJ}Z8L-AGENT mentioned.

Thanks @{ZWJ}Z8l-agent for everything!"""

    assert sanitize_agent_mentions(text) == expected


def test_sanitize_with_urls():
    """Test that URLs containing z8l-agent are handled correctly."""
    test_cases = [
        # URL should not be sanitized
        ("Visit https://github.com/z8l-agent", "Visit https://github.com/z8l-agent"),
        # But mention should be sanitized
        (
            "See @z8l-agent at https://github.com/z8l-agent",
            f"See @{ZWJ}z8l-agent at https://github.com/z8l-agent",
        ),
    ]
    for input_text, expected in test_cases:
        assert sanitize_agent_mentions(input_text) == expected


def test_sanitize_preserves_whitespace():
    """Test that whitespace is preserved correctly."""
    text = "  @z8l-agent  \n  @Z8L-AGENT  "
    expected = f"  @{ZWJ}z8l-agent  \n  @{ZWJ}Z8L-AGENT  "
    assert sanitize_agent_mentions(text) == expected


def test_zwj_constant():
    """Test that ZWJ constant is correctly defined."""
    assert ZWJ == "\u200d"
    assert len(ZWJ) == 1
    assert ord(ZWJ) == 0x200D
