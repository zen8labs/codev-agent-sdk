"""Tests for CodeGraph explore tool."""

import os
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
from openhands.sdk.tool.tool import DeclaredResources
from openhands.sdk.workspace import LocalWorkspace
from openhands.tools.codegraph import (
    CodeGraphExploreAction,
    CodeGraphExploreObservation,
    CodegraphExploreTool,
)
from openhands.tools.codegraph.bootstrap import ensure_codegraph_index
from openhands.tools.preset.default import get_default_tools


def _create_test_conv_state(temp_dir: str) -> ConversationState:
    llm = LLM(model="gpt-4o-mini", api_key=SecretStr("test-key"), usage_id="test-llm")
    agent = Agent(llm=llm, tools=[])
    return ConversationState.create(
        id=uuid4(),
        workspace=LocalWorkspace(working_dir=temp_dir),
        agent=agent,
    )


def test_codegraph_tool_initialization():
    with tempfile.TemporaryDirectory() as temp_dir:
        conv_state = _create_test_conv_state(temp_dir)
        tools = CodegraphExploreTool.create(conv_state)

        assert len(tools) == 1
        tool = tools[0]
        assert tool.name == "codegraph_explore"
        assert tool.executor is not None


def test_codegraph_tool_invalid_working_dir():
    with pytest.raises(ValueError, match="not a valid directory"):
        conv_state = _create_test_conv_state("/nonexistent/directory")
        CodegraphExploreTool.create(conv_state)


@patch("openhands.tools.codegraph.impl.resolve_codegraph_bin", return_value=None)
def test_explore_no_binary(mock_resolve):
    with tempfile.TemporaryDirectory() as temp_dir:
        conv_state = _create_test_conv_state(temp_dir)
        tool = CodegraphExploreTool.create(conv_state)[0]
        action = CodeGraphExploreAction(query="how does auth work")
        observation = tool.executor(action)

        assert isinstance(observation, CodeGraphExploreObservation)
        assert observation.is_error is True
        assert "not installed" in observation.text.lower()


@patch("openhands.tools.codegraph.impl.resolve_codegraph_bin", return_value="/usr/bin/codegraph")
def test_explore_no_index(mock_resolve):
    with tempfile.TemporaryDirectory() as temp_dir:
        conv_state = _create_test_conv_state(temp_dir)
        tool = CodegraphExploreTool.create(conv_state)[0]
        action = CodeGraphExploreAction(query="how does auth work")
        observation = tool.executor(action)

        assert observation.is_error is True
        assert "codegraph init" in observation.text.lower()


@patch("openhands.tools.codegraph.impl.resolve_codegraph_bin", return_value="/usr/bin/codegraph")
@patch("openhands.tools.codegraph.impl.subprocess.run")
def test_explore_success(mock_run, mock_resolve):
    with tempfile.TemporaryDirectory() as temp_dir:
        (Path(temp_dir) / ".codegraph").mkdir()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="class AuthService:\n    def login(self): ...",
            stderr="",
        )

        conv_state = _create_test_conv_state(temp_dir)
        tool = CodegraphExploreTool.create(conv_state)[0]
        action = CodeGraphExploreAction(query="how does login work")
        observation = tool.executor(action)

        assert observation.is_error is False
        assert "AuthService" in observation.text
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == ["/usr/bin/codegraph", "explore", "how does login work"]
        assert kwargs["cwd"] == str(Path(temp_dir).resolve())


@patch("openhands.tools.codegraph.impl.resolve_codegraph_bin", return_value="/usr/bin/codegraph")
@patch("openhands.tools.codegraph.impl.subprocess.run")
def test_explore_timeout(mock_run, mock_resolve):
    with tempfile.TemporaryDirectory() as temp_dir:
        (Path(temp_dir) / ".codegraph").mkdir()
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="codegraph", timeout=120)

        conv_state = _create_test_conv_state(temp_dir)
        tool = CodegraphExploreTool.create(conv_state)[0]
        observation = tool.executor(CodeGraphExploreAction(query="trace flow"))

        assert observation.is_error is True
        assert "timed out" in observation.text.lower()


def test_declared_resources_parallel_safe():
    with tempfile.TemporaryDirectory() as temp_dir:
        tool = CodegraphExploreTool.create(_create_test_conv_state(temp_dir))[0]
        resources = tool.declared_resources(CodeGraphExploreAction(query="x"))
        assert isinstance(resources, DeclaredResources)
        assert resources.declared is True
        assert resources.keys == ()


def test_default_tools_flag(monkeypatch):
    monkeypatch.delenv("OH_ENABLE_CODEGRAPH", raising=False)
    names = [tool.name for tool in get_default_tools(enable_browser=False)]
    assert "codegraph_explore" not in names

    monkeypatch.setenv("OH_ENABLE_CODEGRAPH", "true")
    names = [tool.name for tool in get_default_tools(enable_browser=False)]
    assert "codegraph_explore" in names


@pytest.mark.asyncio
async def test_ensure_codegraph_index_skips_when_disabled(monkeypatch):
    monkeypatch.delenv("OH_ENABLE_CODEGRAPH", raising=False)

    async def execute_command(command, cwd=None, timeout=30.0):
        if command.startswith("test -d "):
            return MagicMock(exit_code=1, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {command!r}")

    result = await ensure_codegraph_index("/tmp/project", execute_command)
    assert result is False


@pytest.mark.asyncio
async def test_ensure_codegraph_index_runs_init(monkeypatch):
    monkeypatch.setenv("OH_ENABLE_CODEGRAPH", "true")
    monkeypatch.setenv("CODEGRAPH_INIT_ON_START", "true")

    with tempfile.TemporaryDirectory() as temp_dir:
        calls: list[tuple[str, str | None]] = []
        state = {"index_created": False}

        async def execute_command(command, cwd=None, timeout=30.0):
            calls.append((command, cwd))
            if command.startswith("test -d "):
                return MagicMock(
                    exit_code=1 if not state["index_created"] else 0,
                    stdout="",
                    stderr="",
                )
            if command == "codegraph --version":
                return MagicMock(exit_code=0, stdout="1.0.1\n", stderr="")
            if command == "codegraph init":
                state["index_created"] = True
                (Path(cwd or temp_dir) / ".codegraph").mkdir()
                return MagicMock(exit_code=0, stdout="", stderr="")
            raise AssertionError(f"unexpected command: {command!r}")

        result = await ensure_codegraph_index(temp_dir, execute_command)

        assert result is True
        assert calls == [
            (f"test -d {temp_dir}/.codegraph", None),
            ("codegraph --version", None),
            ("codegraph init", temp_dir),
            (f"test -d {temp_dir}/.codegraph", None),
        ]


@pytest.mark.asyncio
async def test_ensure_codegraph_index_skips_when_cli_missing_in_sandbox(monkeypatch):
    monkeypatch.setenv("OH_ENABLE_CODEGRAPH", "true")
    monkeypatch.setenv("CODEGRAPH_INIT_ON_START", "true")

    async def execute_command(command, cwd=None, timeout=30.0):
        if command.startswith("test -d "):
            return MagicMock(exit_code=1, stdout="", stderr="")
        if command == "codegraph --version":
            return MagicMock(exit_code=127, stdout="", stderr="not found")
        raise AssertionError(f"unexpected command: {command!r}")

    result = await ensure_codegraph_index("/workspace/project/nexo", execute_command)
    assert result is False
