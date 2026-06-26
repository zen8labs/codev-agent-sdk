from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

import httpx
from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices
from pydantic import Field, PrivateAttr

from openhands.sdk.agent.base import AgentBase
from openhands.sdk.context.agent_context import AgentContext
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.event import (
    ACPToolCallEvent,
    ActionEvent,
    MessageEvent,
    ObservationEvent,
)
from openhands.sdk.event.conversation_error import ConversationErrorEvent
from openhands.sdk.llm import LLM, Message, MessageToolCall, TextContent
from openhands.sdk.logger import get_logger
from openhands.sdk.observability.laminar import maybe_init_laminar, observe
from openhands.sdk.tool.builtins.finish import FinishAction, FinishObservation
from openhands.sdk.utils import maybe_truncate
from openhands.sdk.utils.async_executor import AsyncExecutor


if TYPE_CHECKING:
    from openhands.sdk.conversation import (
        ConversationCallbackType,
        ConversationState,
        ConversationTokenCallbackType,
        LocalConversation,
    )


logger = get_logger(__name__)
maybe_init_laminar()

_DEFAULT_PROMPT_TIMEOUT = float(os.environ.get("OPENCODE_PROMPT_TIMEOUT", "1800"))
_DEFAULT_HTTP_TIMEOUT = float(os.environ.get("OPENCODE_HTTP_TIMEOUT", "30"))
_ACTIVITY_SIGNAL_INTERVAL = 30.0
_MAX_TOOL_CONTENT_CHARS = 30_000


def _state_dir_from_env() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base).expanduser() / "opencode"
    return Path.home() / ".local" / "state" / "opencode"


def _first_string(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _command_has_port(command: list[str]) -> bool:
    """Return True if ``command`` already specifies an explicit ``--port``."""
    for i, arg in enumerate(command):
        if arg == "--port" and i + 1 < len(command):
            return True
        # ``--port=NNNN`` form
        if arg.startswith("--port=") and len(arg) > len("--port="):
            return True
    return False


def _pick_free_port() -> int:
    """Bind to port 0 to let the OS pick a free TCP port, then release it.

    There is an inherent TOCTOU race (the port could be reclaimed before
    ``opencode serve`` binds it); the readiness loop in ``_ensure_server_ready``
    retries for 30s, so a collision is recovered from rather than fatal.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _extract_session_id(payload: Any) -> str | None:
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    if not isinstance(payload, dict):
        return None
    for key in ("session_id", "sessionId", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = payload.get("session")
    if isinstance(nested, dict):
        return _extract_session_id(nested)
    return None


class OpenCodeAgent(AgentBase):
    """Native REST/SSE OpenCode adapter."""

    llm: LLM = Field(
        default_factory=lambda: LLM(model="opencode-managed"),
        description="Attribution-only LLM identity for OpenCode conversations.",
    )
    agent_context: AgentContext | None = Field(
        default=None,
        description="Optional prompt-only context preserved on the agent.",
    )
    opencode_state_dir: str | None = Field(
        default=None,
        description="Optional override for the OpenCode daemon state directory.",
    )
    opencode_http_base: str | None = Field(
        default=None,
        description="Optional override for the OpenCode daemon HTTP base URL.",
    )
    opencode_start_command: list[str] = Field(
        default_factory=list,
        description="Optional explicit command to start the OpenCode daemon.",
    )
    opencode_prompt_timeout: float = Field(
        default=_DEFAULT_PROMPT_TIMEOUT,
        gt=0,
        description="Timeout in seconds for a full OpenCode turn.",
    )
    opencode_model: str | None = Field(
        default=None,
        description=(
            "Model identifier for the OpenCode daemon (e.g. a free OpenCode Zen "
            "model). The deploying application routes this into the daemon's "
            "config via the ``OPENCODE_CONFIG_CONTENT`` env secret."
        ),
    )
    opencode_use_llm_profile: bool = Field(
        default=True,
        description=(
            "When True, the deploying app should route the daemon through the "
            "user's active LLM profile. When False, use the OpenCode Zen gateway "
            "with ``opencode_model``. This field is read by the app-server "
            "builder, not the agent itself."
        ),
    )

    _executor: AsyncExecutor | None = PrivateAttr(default=None)
    _base_url: str | None = PrivateAttr(default=None)
    _auth_header: str | None = PrivateAttr(default=None)
    _closed: bool = PrivateAttr(default=False)
    _on_activity: Callable[[], None] | None = PrivateAttr(default=None)
    _last_activity_signal_at: float = PrivateAttr(default=0.0)
    _subprocess_env: dict[str, str] | None = PrivateAttr(default=None)
    _subagent_emit_state: dict[str, str | None] = PrivateAttr(default_factory=dict)
    _seen_part_texts: dict[str, str] = PrivateAttr(default_factory=dict)
    _child_session_ids: set[str] = PrivateAttr(default_factory=set)
    # Port chosen by ``_start_server`` when the start command did not already
    # specify one. Stored so ``_try_port_from_command`` can rediscover the
    # daemon without relying on ``server.json`` (which ``opencode serve`` does
    # not write in current builds).
    _chosen_port: int | None = PrivateAttr(default=None)

    @property
    def supports_openhands_tools(self) -> bool:
        return False

    @property
    def supports_openhands_mcp(self) -> bool:
        return False

    @property
    def supports_condenser(self) -> bool:
        return False

    @property
    def agent_kind(self) -> Literal["opencode"]:
        return "opencode"

    @property
    def emits_native_stream_tokens(self) -> bool:
        return True

    @property
    def initialize_on_send_message(self) -> bool:
        return False

    @property
    def supports_activity_heartbeat(self) -> bool:
        return True

    def init_state(
        self,
        state: ConversationState,
        on_event: ConversationCallbackType,  # noqa: ARG002
    ) -> None:
        self._ensure_runtime()
        assert self._executor is not None
        self._executor.run_async(
            self._ainit_state(state), timeout=self.opencode_prompt_timeout
        )

    async def _ainit_state(self, state: ConversationState) -> None:
        # Resolve conversation secrets into env vars for the OpenCode daemon.
        # The deploying application delivers model/provider routing via
        # ``OPENCODE_CONFIG_CONTENT`` (and ``OPENCODE_API_KEY`` for Zen free
        # models) as conversation secrets; the daemon reads them at startup.
        self._subprocess_env = state.secret_registry.get_all_secrets_as_env_vars()

        base_url, auth_header = await self._ensure_server_ready()
        self._base_url = base_url
        self._auth_header = auth_header

        cwd = str(state.workspace.working_dir) if state.workspace else os.getcwd()
        prior_session_id = state.agent_state.get("opencode_session_id")
        prior_session_cwd = state.agent_state.get("opencode_session_cwd")
        if (
            isinstance(prior_session_id, str)
            and prior_session_id.strip()
            and prior_session_cwd == cwd
        ):
            session_id = prior_session_id.strip()
        else:
            session_id = await self._create_session(base_url, auth_header, cwd)

        state.agent_state = {
            **state.agent_state,
            "opencode_session_id": session_id,
            "opencode_session_cwd": cwd,
        }

    @observe(name="opencode_agent.step")
    def step(
        self,
        conversation: LocalConversation,
        on_event: ConversationCallbackType,
        on_token: ConversationTokenCallbackType | None = None,
    ) -> None:
        self._ensure_runtime()
        assert self._executor is not None
        self._executor.run_async(
            self._astep_impl(conversation, on_event, on_token),
            timeout=self.opencode_prompt_timeout,
        )

    async def astep(
        self,
        conversation: LocalConversation,
        on_event: ConversationCallbackType,
        on_token: ConversationTokenCallbackType | None = None,
    ) -> None:
        await self._astep_impl(conversation, on_event, on_token)

    async def _astep_impl(
        self,
        conversation: LocalConversation,
        on_event: ConversationCallbackType,
        on_token: ConversationTokenCallbackType | None,
    ) -> None:
        state = conversation.state
        await self._ainit_state(state)

        session_id = state.agent_state["opencode_session_id"]
        prompt_text = self._latest_user_prompt(state)
        if not prompt_text:
            logger.warning("OpenCode step skipped: no user message found")
            state.execution_status = ConversationExecutionStatus.FINISHED
            return

        mask = state.secret_registry.mask_secrets_in_output
        cwd = state.agent_state.get("opencode_session_cwd", "")
        self._signal_activity()
        self._subagent_emit_state = {}
        self._seen_part_texts = {}
        self._child_session_ids = set()

        done_event = asyncio.Event()
        error_msg: list[str | None] = [None]

        sse_task = asyncio.ensure_future(
            self._consume_sse_stream(
                session_id, cwd, on_event, on_token, mask, done_event, error_msg
            )
        )

        try:
            await self._send_prompt_async(session_id, prompt_text)
        except Exception as exc:
            logger.error("OpenCode prompt_async failed: %s", exc, exc_info=True)
            sse_task.cancel()
            try:
                await sse_task
            except (asyncio.CancelledError, Exception):
                pass
            on_event(
                MessageEvent(
                    source="agent",
                    llm_message=Message(
                        role="assistant",
                        content=[TextContent(text=f"OpenCode error: {exc}")],
                    ),
                )
            )
            on_event(
                ConversationErrorEvent(
                    source="agent",
                    code="OpenCodePromptError",
                    detail=str(exc)[:500],
                )
            )
            state.execution_status = ConversationExecutionStatus.ERROR
            return

        try:
            await asyncio.wait_for(
                done_event.wait(), timeout=self.opencode_prompt_timeout
            )
        except TimeoutError:
            logger.warning(
                "OpenCode turn timed out after %ss", self.opencode_prompt_timeout
            )
            error_msg[0] = (
                f"OpenCode turn timed out after {self.opencode_prompt_timeout}s"
            )
        finally:
            sse_task.cancel()
            try:
                await sse_task
            except (asyncio.CancelledError, Exception):
                pass

        if error_msg[0]:
            on_event(
                MessageEvent(
                    source="agent",
                    llm_message=Message(
                        role="assistant",
                        content=[TextContent(text=f"OpenCode error: {error_msg[0]}")],
                    ),
                )
            )
            on_event(
                ConversationErrorEvent(
                    source="agent",
                    code="OpenCodeModelError",
                    detail=error_msg[0][:500],
                )
            )
            state.execution_status = ConversationExecutionStatus.ERROR
            return

        self._emit_subagent_tool_call_events(
            self._fetch_subagent_tool_call_events(session_id, mask), on_event
        )

        response_data = await self._fetch_final_response(session_id)

        info = response_data.get("info", {}) if isinstance(response_data, dict) else {}
        error_info = info.get("error") if isinstance(info, dict) else None
        if isinstance(error_info, dict):
            error_msg_text = _first_string(error_info, "message", "error", "detail")
            if not error_msg_text:
                nested = error_info.get("data")
                if isinstance(nested, dict):
                    error_msg_text = _first_string(nested, "message", "error", "detail")
            if not error_msg_text:
                error_msg_text = str(error_info)
            on_event(
                MessageEvent(
                    source="agent",
                    llm_message=Message(
                        role="assistant",
                        content=[
                            TextContent(text=f"OpenCode model error: {error_msg_text}")
                        ],
                    ),
                )
            )
            on_event(
                ConversationErrorEvent(
                    source="agent",
                    code="OpenCodeModelError",
                    detail=error_msg_text[:500],
                )
            )
            state.execution_status = ConversationExecutionStatus.ERROR
            return

        parts = (
            response_data.get("parts", []) if isinstance(response_data, dict) else []
        )
        stream_text: list[str] = []
        stream_reasoning: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type", "")
            if part_type == "text":
                text = mask(part.get("text", ""))
                if text:
                    stream_text.append(text)
            elif part_type == "reasoning":
                text = mask(part.get("text", ""))
                if text:
                    stream_reasoning.append(text)

        response_text = mask("".join(stream_text))
        reasoning_text = mask("".join(stream_reasoning))
        if not response_text:
            response_text = "(No response from OpenCode)"

        tc_id = str(uuid.uuid4())
        finish_action = FinishAction(message=response_text)
        action_event = ActionEvent(
            source="agent",
            thought=[],
            reasoning_content=reasoning_text or None,
            action=finish_action,
            tool_name="finish",
            tool_call_id=tc_id,
            tool_call=MessageToolCall(
                id=tc_id,
                name="finish",
                arguments=json.dumps({"message": response_text}),
                origin="completion",
            ),
            llm_response_id=str(uuid.uuid4()),
        )
        on_event(action_event)
        on_event(
            ObservationEvent(
                observation=FinishObservation.from_text(text=finish_action.message),
                action_id=action_event.id,
                tool_name="finish",
                tool_call_id=tc_id,
            )
        )
        state.execution_status = ConversationExecutionStatus.FINISHED

    async def _ensure_server_ready(self) -> tuple[str, str | None]:
        base_url, auth_header = self._load_server_credentials()
        if base_url and await self._ping(base_url, auth_header):
            return base_url, auth_header

        self._start_server()
        deadline = time.monotonic() + min(self.opencode_prompt_timeout, 30.0)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                base_url, auth_header = self._load_server_credentials()
                if base_url and await self._ping(base_url, auth_header):
                    return base_url, auth_header
                # If base_url is None (no server.json yet and no
                # opencode_http_base override), try extracting a port from
                # the start command and pinging it directly.  This covers
                # the fresh-daemon race where server.json hasn't been
                # written yet.  ``_start_server`` ensures a ``--port`` is
                # present on the command (current ``opencode serve`` builds
                # never write ``server.json``, so without an explicit port
                # the daemon would be undiscoverable).
                if base_url is None:
                    fallback_url = self._try_port_from_command()
                    if fallback_url and await self._ping(fallback_url, auth_header):
                        return fallback_url, auth_header
            except Exception as exc:
                last_error = exc
            await asyncio.sleep(0.5)

        if last_error is not None:
            raise RuntimeError("OpenCode daemon did not become ready") from last_error
        raise RuntimeError("OpenCode daemon did not become ready")

    def _load_server_credentials(self) -> tuple[str | None, str | None]:
        state_dir = (
            Path(self.opencode_state_dir).expanduser()
            if self.opencode_state_dir
            else _state_dir_from_env()
        )
        server_json = state_dir / "server.json"
        password_file = state_dir / "password"

        if self.opencode_http_base:
            base_url = self.opencode_http_base.rstrip("/")
        elif server_json.exists():
            payload = json.loads(server_json.read_text())
            base_url = _first_string(
                payload,
                "url",
                "base_url",
                "baseUrl",
                "server_url",
                "serverUrl",
            )
            if not base_url:
                host = _first_string(payload, "host", "hostname") or "127.0.0.1"
                port = payload.get("port")
                if isinstance(port, int):
                    base_url = f"http://{host}:{port}"
        else:
            base_url = None

        auth_header = None
        if password_file.exists():
            password = password_file.read_text().strip()
            if password:
                auth_header = f"Bearer {password}"

        if base_url:
            base_url = base_url.rstrip("/")
        return base_url, auth_header

    def _try_port_from_command(self) -> str | None:
        """Discover the daemon's HTTP URL from its start command.

        Looks for an explicit ``--port N`` in ``opencode_start_command`` first
        (covers user-supplied commands), then falls back to the port chosen by
        ``_start_server`` when it augmented the default command with
        ``--port`` (covers the auto-spawned default, since current
        ``opencode serve`` builds do not write ``server.json``).
        """
        for i, arg in enumerate(self.opencode_start_command):
            if arg == "--port" and i + 1 < len(self.opencode_start_command):
                port_str = self.opencode_start_command[i + 1]
                if port_str.isdigit():
                    return f"http://127.0.0.1:{port_str}"
        if self._chosen_port is not None:
            return f"http://127.0.0.1:{self._chosen_port}"
        return None

    async def _ping(self, base_url: str, auth_header: str | None) -> bool:
        headers = self._headers(auth_header)
        async with httpx.AsyncClient(timeout=_DEFAULT_HTTP_TIMEOUT) as client:
            for path, min_status, max_status in (
                ("/health", 200, 299),
                ("/api/health", 200, 299),
                ("/", 200, 499),
            ):
                try:
                    response = await client.get(f"{base_url}{path}", headers=headers)
                    if min_status <= response.status_code <= max_status:
                        return True
                except httpx.HTTPError:
                    continue
        return False

    def _start_server(self) -> None:
        command = list(self.opencode_start_command) or ["opencode", "serve"]
        # ``opencode serve`` does not write ``server.json``/``password`` in
        # current builds, so ``_load_server_credentials`` cannot rediscover an
        # auto-spawned daemon. Ensure an explicit ``--port`` is present and
        # remember it on ``self`` so ``_try_port_from_command`` can ping the
        # daemon directly. Skip the rewrite only when the caller already
        # pinned a port.
        if not _command_has_port(command):
            port = _pick_free_port()
            command = [*command, "--port", str(port)]
            self._chosen_port = port
        else:
            self._chosen_port = None
        fallback = ["npx", "-y", "@opencode-ai/cli", "serve"]
        if not _command_has_port(fallback):
            fallback = [
                *fallback,
                "--port",
                str(self._chosen_port or _pick_free_port()),
            ]
        env = {**os.environ, **(self._subprocess_env or {})}
        try:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
            logger.info("Started OpenCode daemon with %s", command)
        except FileNotFoundError:
            subprocess.Popen(
                fallback,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
            logger.info("Started OpenCode daemon with %s", fallback)

    async def _create_session(
        self,
        base_url: str,
        auth_header: str | None,
        cwd: str,
    ) -> str:
        async with httpx.AsyncClient(timeout=self.opencode_prompt_timeout) as client:
            headers = self._headers(auth_header)
            endpoints = [
                ("POST", f"{base_url}/session?directory={quote(cwd, safe='')}"),
                ("POST", f"{base_url}/api/session?directory={quote(cwd, safe='')}"),
                ("POST", f"{base_url}/api/session"),
            ]
            for method, url in endpoints:
                body = None
                if url.endswith("/api/session"):
                    body = {"directory": cwd}
                try:
                    response = await client.request(
                        method, url, headers=headers, json=body
                    )
                    response.raise_for_status()
                    session_id = _extract_session_id(response.json())
                    if session_id:
                        return session_id
                except httpx.HTTPError:
                    continue
        raise RuntimeError("Failed to create OpenCode session")

    def _latest_user_prompt(self, state: ConversationState) -> str:
        last_id = state.last_user_message_id
        for event in reversed(state.events):
            if not isinstance(event, MessageEvent) or event.source != "user":
                continue
            if last_id is not None and event.id != last_id:
                continue
            parts = [
                content.text
                for content in event.to_llm_message().content
                if isinstance(content, TextContent)
            ]
            if parts:
                return "\n".join(parts)
        return ""

    def _headers(self, auth_header: str | None) -> dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream"}
        if auth_header:
            headers["Authorization"] = auth_header
        return headers

    def _ensure_runtime(self) -> None:
        if self._closed:
            raise RuntimeError("OpenCodeAgent has been closed")
        if self._executor is None:
            self._executor = AsyncExecutor()

    def _signal_activity(self) -> None:
        callback = self._on_activity
        if callback is None:
            return
        now = time.monotonic()
        if now - self._last_activity_signal_at < _ACTIVITY_SIGNAL_INTERVAL:
            return
        self._last_activity_signal_at = now
        try:
            callback()
        except Exception:
            logger.debug("OpenCode activity callback failed", exc_info=True)

    async def _send_prompt_async(self, session_id: str, prompt_text: str) -> None:
        """Send a prompt via the non-blocking prompt_async endpoint."""
        assert self._base_url is not None
        headers = self._headers(self._auth_header)
        payload = {"parts": [{"type": "text", "text": prompt_text}]}
        url = f"{self._base_url}/session/{session_id}/prompt_async"
        async with httpx.AsyncClient(timeout=_DEFAULT_HTTP_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()

    async def _fetch_final_response(self, session_id: str) -> dict[str, Any]:
        """Fetch the latest assistant message after the turn completes."""
        assert self._base_url is not None
        headers = self._headers(self._auth_header)
        url = f"{self._base_url}/session/{session_id}/message"
        async with httpx.AsyncClient(timeout=_DEFAULT_HTTP_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
            if response.status_code >= 400:
                return {}
            messages = response.json()
            if not isinstance(messages, list):
                return {}
            for msg in reversed(messages):
                info = msg.get("info", {})
                if isinstance(info, dict) and info.get("role") == "assistant":
                    return msg
            return {}

    async def _consume_sse_stream(
        self,
        session_id: str,
        cwd: str,
        on_event: ConversationCallbackType,
        on_token: ConversationTokenCallbackType | None,
        mask: Callable[[str], str],
        done_event: asyncio.Event,
        error_msg: list[str | None],
    ) -> None:
        """Connect to OpenCode's SSE event stream and relay progress live.

        Uses ``GET /event?directory=<cwd>`` — the ``directory`` query parameter
        is required by OpenCode's workspace routing middleware to deliver
        events for the correct project.  Processes ``message.part.updated``,
        ``message.part.delta``, and ``session.idle`` events for our session,
        streaming tool calls, text, and reasoning to the UI in real-time.
        """
        assert self._base_url is not None
        headers = self._headers(self._auth_header)
        url = f"{self._base_url}/event"
        params = {"directory": cwd} if cwd else None
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.opencode_prompt_timeout, connect=10.0)
            ) as client:
                async with client.stream(
                    "GET", url, headers=headers, params=params
                ) as response:
                    if response.status_code >= 400:
                        logger.warning(
                            "OpenCode SSE stream returned %d", response.status_code
                        )
                        return
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload_str = line[5:].strip()
                        if not payload_str:
                            continue
                        try:
                            event = json.loads(payload_str)
                        except json.JSONDecodeError:
                            continue
                        self._handle_sse_event(
                            event,
                            session_id,
                            on_event,
                            on_token,
                            mask,
                            done_event,
                            error_msg,
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("OpenCode SSE stream ended", exc_info=True)

    def _handle_sse_event(
        self,
        event: dict[str, Any],
        session_id: str,
        on_event: ConversationCallbackType,
        on_token: ConversationTokenCallbackType | None,
        mask: Callable[[str], str],
        done_event: asyncio.Event,
        error_msg: list[str | None],
    ) -> None:
        """Process a single SSE event from the OpenCode event stream.

        Events from the main session drive tool calls, text streaming, and
        completion.  Events from child (subagent) sessions are also processed
        so that subagent tool calls appear live in the UI, not just at the end.
        """
        event_type = event.get("type", "")
        props = event.get("properties", {})
        if not isinstance(props, dict):
            return

        event_session_id = props.get("sessionID")
        is_main = event_session_id == session_id
        is_child = (
            event_session_id is not None and event_session_id in self._child_session_ids
        )

        if not is_main and not is_child:
            # Track new child sessions via session.updated events that
            # carry a parentSessionID matching our main session.
            if event_type == "session.updated":
                parent = props.get("parentSessionID")
                if parent == session_id and event_session_id:
                    self._child_session_ids.add(event_session_id)
            return

        if event_type == "message.part.updated":
            part = props.get("part", {})
            if not isinstance(part, dict):
                return
            if is_main:
                self._handle_part_updated(part, on_event, on_token, mask)
            elif is_child and event_session_id is not None:
                self._handle_child_part_updated(part, event_session_id, on_event, mask)
        elif event_type == "message.part.delta":
            if is_main:
                self._handle_part_delta(props, on_token, mask)
        elif event_type == "session.idle":
            if is_main:
                self._signal_activity()
                self._emit_subagent_tool_call_events(
                    self._fetch_subagent_tool_call_events(session_id, mask), on_event
                )
                done_event.set()
            elif is_child:
                self._signal_activity()
        elif event_type == "session.error":
            if is_main:
                error = props.get("error")
                if isinstance(error, dict):
                    error_msg[0] = _first_string(error, "message", "error", "detail")
                elif isinstance(error, str):
                    error_msg[0] = error
                if not error_msg[0]:
                    error_msg[0] = "Unknown OpenCode session error"
                done_event.set()
        elif event_type in (
            "message.updated",
            "session.updated",
            "session.status",
        ):
            self._signal_activity()

    def _handle_child_part_updated(
        self,
        part: dict[str, Any],
        child_session_id: str,
        on_event: ConversationCallbackType,
        mask: Callable[[str], str],
    ) -> None:
        """Handle a ``message.part.updated`` event from a child (subagent) session.

        Converts tool parts to ACPToolCallEvent tagged with the child's
        session ID so the frontend groups them into a subagent panel.
        """
        part_type = part.get("type", "")
        if part_type == "tool":
            self._emit_tool_call_from_part(
                part, on_event, mask, subagent_session_id=child_session_id
            )
            self._signal_activity()

    def _handle_part_delta(
        self,
        props: dict[str, Any],
        on_token: ConversationTokenCallbackType | None,
        mask: Callable[[str], str],
    ) -> None:
        """Handle a ``message.part.delta`` event for incremental text."""
        if on_token is None:
            return
        delta = props.get("delta", "")
        field = props.get("field", "text")
        if not isinstance(delta, str) or not delta:
            return
        delta = mask(delta)
        if not delta:
            return
        if field == "text":
            on_token(
                ModelResponseStream(
                    choices=[StreamingChoices(index=0, delta=Delta(content=delta))]
                )
            )
        elif field == "reasoning":
            on_token(
                ModelResponseStream(
                    choices=[
                        StreamingChoices(
                            index=0, delta=Delta(content=None, reasoning_content=delta)
                        )
                    ]
                )
            )
        self._signal_activity()

    def _handle_part_updated(
        self,
        part: dict[str, Any],
        on_event: ConversationCallbackType,
        on_token: ConversationTokenCallbackType | None,
        mask: Callable[[str], str],
    ) -> None:
        """Handle a ``message.part.updated`` event for a single part."""
        part_type = part.get("type", "")
        part_id = part.get("id", "")

        if part_type == "tool":
            self._emit_tool_call_from_part(part, on_event, mask)
            self._signal_activity()
        elif part_type in ("text", "reasoning"):
            self._stream_part_text_delta(part_id, part_type, part, on_token, mask)
            self._signal_activity()
        elif part_type in ("step-start", "step-finish", "subtask"):
            self._signal_activity()

    def _stream_part_text_delta(
        self,
        part_id: str,
        part_type: str,
        part: dict[str, Any],
        on_token: ConversationTokenCallbackType | None,
        mask: Callable[[str], str],
    ) -> None:
        """Stream incremental text or reasoning from a ``message.part.updated``.

        OpenCode sends the full accumulated text on each ``part.updated``,
        so we track the previously seen text per part ID and only emit the
        delta.  Used for both ``text`` and ``reasoning`` part types.
        """
        if on_token is None:
            return
        text = mask(part.get("text", ""))
        if not text:
            return
        prev = self._seen_part_texts.get(part_id, "")
        delta_text = text[len(prev) :] if text.startswith(prev) else text
        self._seen_part_texts[part_id] = text
        if not delta_text:
            return
        if part_type == "text":
            on_token(
                ModelResponseStream(
                    choices=[StreamingChoices(index=0, delta=Delta(content=delta_text))]
                )
            )
        else:
            on_token(
                ModelResponseStream(
                    choices=[
                        StreamingChoices(
                            index=0,
                            delta=Delta(content=None, reasoning_content=delta_text),
                        )
                    ]
                )
            )

    def _emit_tool_call_from_part(
        self,
        part: dict[str, Any],
        on_event: ConversationCallbackType,
        mask: Callable[[str], str],
        subagent_session_id: str | None = None,
    ) -> None:
        """Convert an OpenCode ToolPart to an ACPToolCallEvent and emit it."""
        event = self._tool_part_to_acp_event(part, mask, subagent_session_id)
        if event is None:
            return
        emit_key = f"{subagent_session_id or 'main'}:{event.tool_call_id}"
        if self._subagent_emit_state.get(emit_key) == event.status:
            return
        self._subagent_emit_state[emit_key] = event.status
        try:
            on_event(event)
        except Exception:
            logger.debug("Failed to emit tool call event", exc_info=True)

    @staticmethod
    def _map_tool_status(status: str) -> str:
        return {
            "completed": "completed",
            "error": "failed",
            "running": "in_progress",
            "pending": "in_progress",
        }.get(status, status)

    @staticmethod
    def _tool_part_to_acp_event(
        part: dict[str, Any],
        mask: Callable[[str], str],
        subagent_session_id: str | None = None,
        agent_name: str | None = None,
    ) -> ACPToolCallEvent | None:
        """Convert an OpenCode ToolPart dict to an ACPToolCallEvent.

        Shared by both the SSE handler (live) and the REST poller (subagent
        sweep).  Returns None if the part lacks a call ID.
        """
        call_id = part.get("callID", part.get("id", ""))
        if not call_id:
            return None
        state = part.get("state", {})
        if not isinstance(state, dict):
            return None
        status = state.get("status", "completed")
        tool_name = part.get("tool", "")
        title = state.get("title") or tool_name
        raw_input = state.get("input")
        raw_output = state.get("output") or state.get("error")
        if isinstance(raw_output, str):
            raw_output = maybe_truncate(
                mask(raw_output), truncate_after=_MAX_TOOL_CONTENT_CHARS
            )
        if isinstance(title, str):
            title = mask(title)
        if isinstance(raw_input, str):
            raw_input = mask(raw_input)
        mapped_status = OpenCodeAgent._map_tool_status(status)
        return ACPToolCallEvent(
            tool_call_id=str(call_id),
            title=title,
            status=mapped_status,
            tool_kind=None,
            raw_input=raw_input,
            raw_output=raw_output,
            content=None,
            is_error=(mapped_status == "failed"),
            subagent_session_id=subagent_session_id,
            agent_name=agent_name,
        )

    def _fetch_subagent_tool_call_events(
        self,
        session_id: str,
        mask: Callable[[str], str],
    ) -> list[ACPToolCallEvent]:
        """Fetch ACPToolCallEvents for OpenCode subagent sessions via REST API.

        Mirrors the ACP agent's approach: fetch child sessions, then for each
        child fetch its messages and extract tool-call events, prompt, and
        response — all tagged with the child's session id so the frontend
        groups them into a subagent panel.
        """
        events: list[ACPToolCallEvent] = []
        if not self._base_url:
            return events
        base = self._base_url
        req_headers: dict[str, str] = {}
        if self._auth_header:
            req_headers["Authorization"] = self._auth_header
        try:
            req = urllib.request.Request(
                f"{base}/session/{session_id}/children",
                headers=req_headers,
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                children: list[dict[str, Any]] = json.loads(resp.read())
            if not children:
                return events
        except Exception as exc:
            logger.debug(
                "Could not fetch subagent children from OpenCode HTTP API: %s", exc
            )
            return events

        for child in children:
            child_id = child.get("id")
            if not child_id:
                continue
            agent_name = child.get("agent") or child.get("title") or child_id
            child_title = child.get("title") or agent_name or str(child_id)
            assert isinstance(child_title, str)

            try:
                req = urllib.request.Request(
                    f"{base}/session/{child_id}/message",
                    headers=req_headers,
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    messages: list[dict[str, Any]] = json.loads(resp.read())
            except Exception as exc:
                logger.debug(
                    "Could not fetch messages for subagent %s: %s", child_id, exc
                )
                messages = []

            tool_events: list[ACPToolCallEvent] = []
            prompt_text = ""
            response_chunks: list[str] = []
            for msg_envelope in messages:
                info = msg_envelope.get("info", {})
                role = (
                    info.get("role")
                    if isinstance(info, dict)
                    else msg_envelope.get("role")
                )
                parts: list[dict[str, Any]] = msg_envelope.get("parts", [])
                for part in parts:
                    ptype = part.get("type")
                    if ptype == "text":
                        text = part.get("text")
                        if not isinstance(text, str) or not text.strip():
                            continue
                        if role == "user" and not prompt_text:
                            prompt_text = text
                        elif role == "assistant":
                            response_chunks.append(text)
                        continue
                    if ptype != "tool":
                        continue
                    event = self._tool_part_to_acp_event(
                        part,
                        mask,
                        subagent_session_id=child_id,
                        agent_name=agent_name,
                    )
                    if event is not None:
                        tool_events.append(event)

            response_text = "".join(response_chunks).strip()
            session_active = (
                any(te.status == "in_progress" for te in tool_events)
                or not response_text
            )

            events.append(
                ACPToolCallEvent(
                    tool_call_id=f"session:{child_id}",
                    title=child_title,
                    status="in_progress" if session_active else "completed",
                    tool_kind=None,
                    raw_input=None,
                    raw_output=None,
                    content=None,
                    is_error=False,
                    subagent_session_id=child_id,
                    agent_name=agent_name,
                )
            )
            if prompt_text:
                events.append(
                    ACPToolCallEvent(
                        tool_call_id=f"prompt:{child_id}",
                        title="Prompt",
                        status="completed",
                        tool_kind=None,
                        raw_input=None,
                        raw_output=maybe_truncate(
                            mask(prompt_text), truncate_after=_MAX_TOOL_CONTENT_CHARS
                        ),
                        content=None,
                        is_error=False,
                        subagent_session_id=child_id,
                        agent_name=agent_name,
                    )
                )
            events.extend(tool_events)
            if response_text:
                events.append(
                    ACPToolCallEvent(
                        tool_call_id=f"response:{child_id}",
                        title="Response",
                        status="completed",
                        tool_kind=None,
                        raw_input=None,
                        raw_output=maybe_truncate(
                            mask(response_text),
                            truncate_after=_MAX_TOOL_CONTENT_CHARS,
                        ),
                        content=None,
                        is_error=False,
                        subagent_session_id=child_id,
                        agent_name=agent_name,
                    )
                )
        return events

    def _emit_subagent_tool_call_events(
        self,
        events: list[ACPToolCallEvent],
        on_event: ConversationCallbackType,
    ) -> None:
        """Emit subagent tool-call events, skipping ones already sent unchanged."""
        for event in events:
            key = f"{event.subagent_session_id or 'main'}:{event.tool_call_id}"
            if self._subagent_emit_state.get(key) == event.status:
                continue
            self._subagent_emit_state[key] = event.status
            try:
                on_event(event)
            except Exception:
                logger.debug(
                    "Failed to emit subagent tool call event for %s",
                    key,
                    exc_info=True,
                )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._executor is not None:
            self._executor.close()
            self._executor = None
