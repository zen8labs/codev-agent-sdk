"""Tests for CodeGraph navigation tools."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import SecretStr

from openhands.sdk.agent import Agent
from openhands.sdk.conversation.state import ConversationState
from openhands.sdk.llm import LLM
from openhands.sdk.workspace import LocalWorkspace
from openhands.tools.codegraph import (
    CODEGRAPH_TOOL_NAMES,
    FindReferencesAction,
    FindReferencesTool,
    GoToDefinitionAction,
    GoToDefinitionTool,
    ListCallersAction,
    ListCallersTool,
    ListCalleesAction,
    ListCalleesTool,
)
from openhands.tools.codegraph.navigation_find_references import (
    FIND_REFERENCES_DISCLAIMER,
    SECTION_CALLERS,
    SECTION_IMPACT,
    SECTION_QUERY,
    merge_find_references_output,
)
from openhands.tools.codegraph.runner import CodeGraphRunResult
from openhands.tools.preset.default import get_default_tools


def _create_test_conv_state(temp_dir: str) -> ConversationState:
    llm = LLM(model="gpt-4o-mini", api_key=SecretStr("test-key"), usage_id="test-llm")
    agent = Agent(llm=llm, tools=[])
    return ConversationState.create(
        id=uuid4(),
        workspace=LocalWorkspace(working_dir=temp_dir),
        agent=agent,
    )


@pytest.mark.parametrize(
    ("tool_cls", "action_cls", "expected_name"),
    [
        (GoToDefinitionTool, GoToDefinitionAction, "go_to_definition"),
        (ListCallersTool, ListCallersAction, "list_callers"),
        (ListCalleesTool, ListCalleesAction, "list_callees"),
        (FindReferencesTool, FindReferencesAction, "find_references"),
    ],
)
def test_navigation_tool_initialization(tool_cls, action_cls, expected_name):
    with tempfile.TemporaryDirectory() as temp_dir:
        tool = tool_cls.create(_create_test_conv_state(temp_dir))[0]
        assert tool.name == expected_name
        assert tool.executor is not None
        action = action_cls(symbol="AuthService")
        observation = tool.executor(action)
        assert observation.is_error is True


@patch("openhands.tools.codegraph.navigation_go_to_definition.validate_codegraph_prerequisites", return_value=(None, "CLI not installed"))
def test_go_to_definition_no_binary(mock_validate):
    with tempfile.TemporaryDirectory() as temp_dir:
        tool = GoToDefinitionTool.create(_create_test_conv_state(temp_dir))[0]
        observation = tool.executor(GoToDefinitionAction(symbol="AuthService"))
        assert observation.is_error is True
        assert "not installed" in observation.text.lower()


@patch("openhands.tools.codegraph.navigation_list_callers.validate_codegraph_prerequisites", return_value=("/usr/bin/codegraph", None))
@patch("openhands.tools.codegraph.navigation_list_callers.run_codegraph_cli")
def test_list_callers_success(mock_run, mock_validate):
    with tempfile.TemporaryDirectory() as temp_dir:
        (Path(temp_dir) / ".codegraph").mkdir()
        mock_run.return_value = CodeGraphRunResult(
            text="main.py:42 calls AuthService.login",
            is_error=False,
            returncode=0,
        )

        tool = ListCallersTool.create(_create_test_conv_state(temp_dir))[0]
        observation = tool.executor(
            ListCallersAction(symbol="login", limit=10)
        )

        assert observation.is_error is False
        mock_run.assert_called_once()
        command = mock_run.call_args.kwargs["command"]
        assert command[:3] == ["/usr/bin/codegraph", "callers", "login"]
        assert "-l" in command and "10" in command


@patch("openhands.tools.codegraph.navigation_list_callees.validate_codegraph_prerequisites", return_value=("/usr/bin/codegraph", None))
@patch("openhands.tools.codegraph.navigation_list_callees.run_codegraph_cli")
def test_list_callees_success(mock_run, mock_validate):
    with tempfile.TemporaryDirectory() as temp_dir:
        (Path(temp_dir) / ".codegraph").mkdir()
        mock_run.return_value = CodeGraphRunResult(
            text="AuthService.login calls validate_token",
            is_error=False,
            returncode=0,
        )

        tool = ListCalleesTool.create(_create_test_conv_state(temp_dir))[0]
        observation = tool.executor(ListCalleesAction(symbol="login"))

        assert observation.is_error is False
        command = mock_run.call_args.kwargs["command"]
        assert command[:3] == ["/usr/bin/codegraph", "callees", "login"]


@patch("openhands.tools.codegraph.navigation_go_to_definition.validate_codegraph_prerequisites", return_value=("/usr/bin/codegraph", None))
@patch("openhands.tools.codegraph.navigation_go_to_definition.run_codegraph_cli")
def test_go_to_definition_with_file(mock_run, mock_validate):
    with tempfile.TemporaryDirectory() as temp_dir:
        (Path(temp_dir) / ".codegraph").mkdir()
        mock_run.return_value = CodeGraphRunResult(
            text="src/auth.py:10 class AuthService",
            is_error=False,
            returncode=0,
        )

        tool = GoToDefinitionTool.create(_create_test_conv_state(temp_dir))[0]
        observation = tool.executor(
            GoToDefinitionAction(symbol="AuthService", file="src/auth.py")
        )

        assert observation.is_error is False
        command = mock_run.call_args.kwargs["command"]
        assert command == [
            "/usr/bin/codegraph",
            "node",
            "AuthService",
            "-f",
            "src/auth.py",
        ]


@patch("openhands.tools.codegraph.navigation_find_references.validate_codegraph_prerequisites", return_value=("/usr/bin/codegraph", None))
@patch("openhands.tools.codegraph.navigation_find_references.run_codegraph_cli_batch")
def test_find_references_runs_three_commands(mock_batch, mock_validate):
    with tempfile.TemporaryDirectory() as temp_dir:
        (Path(temp_dir) / ".codegraph").mkdir()
        mock_batch.return_value = [
            CodeGraphRunResult(text="caller output", is_error=False, returncode=0),
            CodeGraphRunResult(text="impact output", is_error=False, returncode=0),
            CodeGraphRunResult(text="query output", is_error=False, returncode=0),
        ]

        tool = FindReferencesTool.create(_create_test_conv_state(temp_dir))[0]
        observation = tool.executor(FindReferencesAction(symbol="AuthService"))

        assert observation.is_error is False
        mock_batch.assert_called_once()
        commands = mock_batch.call_args.kwargs["commands"]
        assert len(commands) == 3
        assert commands[0][:3] == ["/usr/bin/codegraph", "callers", "AuthService"]
        assert commands[1][:3] == ["/usr/bin/codegraph", "impact", "AuthService"]
        assert commands[2][:3] == ["/usr/bin/codegraph", "query", "AuthService"]
        assert FIND_REFERENCES_DISCLAIMER.splitlines()[0] in observation.text
        assert SECTION_CALLERS in observation.text
        assert SECTION_IMPACT in observation.text
        assert SECTION_QUERY in observation.text


@patch("openhands.tools.codegraph.navigation_find_references.validate_codegraph_prerequisites", return_value=("/usr/bin/codegraph", None))
@patch("openhands.tools.codegraph.navigation_find_references.run_codegraph_cli_batch")
def test_find_references_partial_failure(mock_batch, mock_validate):
    with tempfile.TemporaryDirectory() as temp_dir:
        (Path(temp_dir) / ".codegraph").mkdir()
        mock_batch.return_value = [
            CodeGraphRunResult(text="caller output", is_error=False, returncode=0),
            CodeGraphRunResult(text="impact failed", is_error=True, returncode=1),
            CodeGraphRunResult(text="query output", is_error=False, returncode=0),
        ]

        tool = FindReferencesTool.create(_create_test_conv_state(temp_dir))[0]
        observation = tool.executor(FindReferencesAction(symbol="AuthService"))

        assert observation.is_error is False
        assert "caller output" in observation.text
        assert "impact failed" in observation.text


def test_merge_find_references_output_all_failed():
    merged = merge_find_references_output(
        "Foo",
        [
            CodeGraphRunResult(text="err1", is_error=True, returncode=1),
            CodeGraphRunResult(text="err2", is_error=True, returncode=1),
            CodeGraphRunResult(text="err3", is_error=True, returncode=1),
        ],
    )
    assert "find_references: Foo" in merged
    assert SECTION_CALLERS in merged


def test_default_tools_includes_all_codegraph_tools(monkeypatch):
    monkeypatch.delenv("OH_ENABLE_CODEGRAPH", raising=False)
    names = {tool.name for tool in get_default_tools(enable_browser=False)}
    assert not CODEGRAPH_TOOL_NAMES.intersection(names)

    monkeypatch.setenv("OH_ENABLE_CODEGRAPH", "true")
    names = {tool.name for tool in get_default_tools(enable_browser=False)}
    assert CODEGRAPH_TOOL_NAMES.issubset(names)


def test_enable_codegraph_with_list_callers_only():
    llm = LLM(model="gpt-4o-mini", api_key=SecretStr("test-key"), usage_id="test-llm")
    from openhands.sdk import Agent
    from openhands.sdk.tool import Tool

    agent = Agent(llm=llm, tools=[Tool(name="list_callers")])
    message = agent.static_system_message
    assert "CODEGRAPH_NAVIGATION" in message
