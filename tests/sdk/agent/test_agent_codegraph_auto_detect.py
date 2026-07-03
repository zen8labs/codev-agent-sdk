from __future__ import annotations

import pytest
from pydantic import SecretStr

from openhands.sdk import Agent
from openhands.sdk.llm import LLM
from openhands.sdk.tool import Tool


def _make_llm() -> LLM:
    return LLM(model="test-model", api_key=SecretStr("test-key"), usage_id="test-llm")


@pytest.mark.parametrize(
    "tools, prompt_kwargs, expect_codegraph",
    [
        pytest.param(
            [Tool(name="codegraph_explore")],
            {},
            True,
            id="codegraph_tool_present",
        ),
        pytest.param([], {}, False, id="no_tools"),
        pytest.param(
            [Tool(name="terminal_tool"), Tool(name="file_editor_tool")],
            {},
            False,
            id="other_tools_only",
        ),
        pytest.param(
            [Tool(name="list_callers")],
            {},
            True,
            id="navigation_tool_present",
        ),
        pytest.param(
            [Tool(name="codegraph_explore")],
            {"enable_codegraph": False},
            False,
            id="explicit_override_false",
        ),
    ],
)
def test_codegraph_auto_detect(tools, prompt_kwargs, expect_codegraph):
    """enable_codegraph is inferred from tools in static_system_message (like browser)."""
    agent = Agent(llm=_make_llm(), tools=tools, system_prompt_kwargs=prompt_kwargs)
    msg = agent.static_system_message
    if expect_codegraph:
        assert "<CODEGRAPH_EXPLORATION>" in msg
        assert "<CODEGRAPH_NAVIGATION>" in msg
    else:
        assert "<CODEGRAPH_EXPLORATION>" not in msg
