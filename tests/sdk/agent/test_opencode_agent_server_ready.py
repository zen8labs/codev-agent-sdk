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
