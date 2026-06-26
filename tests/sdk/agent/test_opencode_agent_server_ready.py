from __future__ import annotations

from unittest.mock import MagicMock, patch

from openhands.sdk.agent.opencode_agent import (
    OpenCodeAgent,
    _command_has_port,
    _pick_free_port,
)


def test_command_has_port_detects_both_supported_forms() -> None:
    assert _command_has_port(["opencode", "serve", "--port", "1234"])
    assert _command_has_port(["opencode", "serve", "--port=1234"])
    assert not _command_has_port(["opencode", "serve"])


def test_pick_free_port_returns_positive_port() -> None:
    assert _pick_free_port() > 0


def test_try_port_from_command_uses_explicit_port_before_chosen_port() -> None:
    agent = OpenCodeAgent(
        opencode_start_command=["opencode", "serve", "--port", "9999"]
    )
    agent._chosen_port = 1234

    assert agent._try_port_from_command() == "http://127.0.0.1:9999"


def test_try_port_from_command_falls_back_to_chosen_port() -> None:
    agent = OpenCodeAgent(opencode_start_command=[])
    agent._chosen_port = 4242

    assert agent._try_port_from_command() == "http://127.0.0.1:4242"


def test_start_server_adds_port_and_reuses_it_for_fallback() -> None:
    agent = OpenCodeAgent(opencode_start_command=[])

    with (
        patch("openhands.sdk.agent.opencode_agent._pick_free_port", return_value=4242),
        patch(
            "subprocess.Popen",
            side_effect=[FileNotFoundError(), MagicMock()],
        ) as mock_popen,
    ):
        agent._start_server()

    assert agent._chosen_port == 4242
    primary_command = mock_popen.call_args_list[0].args[0]
    fallback_command = mock_popen.call_args_list[1].args[0]
    assert primary_command == ["opencode", "serve", "--port", "4242"]
    assert fallback_command == [
        "npx",
        "-y",
        "@opencode-ai/cli",
        "serve",
        "--port",
        "4242",
    ]


def test_start_server_preserves_explicit_port() -> None:
    agent = OpenCodeAgent(
        opencode_start_command=["opencode", "serve", "--port", "7777"]
    )

    with patch("subprocess.Popen") as mock_popen:
        agent._start_server()

    assert agent._chosen_port is None
    assert mock_popen.call_args.args[0] == ["opencode", "serve", "--port", "7777"]


def test_start_server_stores_popen_handle() -> None:
    """_start_server must store the Popen handle for later cleanup."""
    agent = OpenCodeAgent(opencode_start_command=["opencode", "serve", "--port", "1"])
    mock_proc = MagicMock()

    with patch("subprocess.Popen", return_value=mock_proc):
        agent._start_server()

    assert agent._server_process is mock_proc


def test_close_terminates_daemon() -> None:
    """close() must terminate the spawned OpenCode daemon."""
    agent = OpenCodeAgent(opencode_start_command=["opencode", "serve", "--port", "1"])
    mock_proc = MagicMock()
    mock_proc.wait.return_value = 0
    agent._server_process = mock_proc

    agent.close()

    mock_proc.terminate.assert_called_once()
    mock_proc.wait.assert_called_once()
    assert agent._server_process is None


def test_close_kills_daemon_on_terminate_timeout() -> None:
    """If terminate doesn't finish in time, close() should kill the process."""
    import subprocess

    agent = OpenCodeAgent(opencode_start_command=["opencode", "serve", "--port", "1"])
    mock_proc = MagicMock()
    mock_proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="opencode", timeout=5),
        0,
    ]
    agent._server_process = mock_proc

    agent.close()

    mock_proc.terminate.assert_called_once()
    mock_proc.kill.assert_called_once()
    assert agent._server_process is None


def test_close_is_idempotent() -> None:
    """Calling close() twice should not error."""
    agent = OpenCodeAgent(opencode_start_command=[])
    agent.close()
    agent.close()
    assert agent._closed


def test_reset_after_timeout_clears_session_state() -> None:
    """_reset_after_timeout must clear cached session/daemon state."""
    agent = OpenCodeAgent(opencode_start_command=[])
    agent._base_url = "http://127.0.0.1:9999"
    agent._auth_header = "Bearer token"

    state = MagicMock()
    state.agent_state = {
        "opencode_session_id": "sess-123",
        "opencode_session_cwd": "/workspace",
    }

    agent._reset_after_timeout(state)

    assert "opencode_session_id" not in state.agent_state
    assert "opencode_session_cwd" not in state.agent_state
    assert agent._base_url is None
    assert agent._auth_header is None
