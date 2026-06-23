from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Callable
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


def _extract_text(payload: Any) -> str | None:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in ("text", "delta", "content", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    nested = payload.get("data")
    if nested is not None:
        return _extract_text(nested)
    return None


def _extract_session_ref(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("session_id", "sessionId", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("session", "sessionID"):
        value = payload.get(key)
        if isinstance(value, dict):
            session_id = _extract_session_ref(value)
            if session_id:
                return session_id
        elif isinstance(value, str) and value.strip():
            return value.strip()
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

    _executor: AsyncExecutor | None = PrivateAttr(default=None)
    _base_url: str | None = PrivateAttr(default=None)
    _auth_header: str | None = PrivateAttr(default=None)
    _closed: bool = PrivateAttr(default=False)
    _on_activity: Callable[[], None] | None = PrivateAttr(default=None)
    _last_activity_signal_at: float = PrivateAttr(default=0.0)

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
        self._executor.run_async(self._ainit_state(state), timeout=self.opencode_prompt_timeout)

    async def _ainit_state(self, state: ConversationState) -> None:
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

        stream_text: list[str] = []
        stream_reasoning: list[str] = []
        pending_tools: dict[str, dict[str, Any]] = {}

        try:
            async with httpx.AsyncClient(timeout=self.opencode_prompt_timeout) as client:
                stream_task = asyncio.create_task(
                    self._stream_events(
                        client,
                        session_id,
                        on_event,
                        on_token,
                        state,
                        stream_text,
                        stream_reasoning,
                        pending_tools,
                    )
                )
                try:
                    await self._post_prompt(client, session_id, prompt_text)
                    wait_payload = await self._wait_for_turn(client, session_id)
                finally:
                    stream_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stream_task
        except Exception as exc:
            logger.error("OpenCode prompt failed: %s", exc, exc_info=True)
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

        mask = state.secret_registry.mask_secrets_in_output
        response_text = mask("".join(stream_text))
        reasoning_text = mask("".join(stream_reasoning))
        if not response_text:
            response_text = mask(_extract_text(wait_payload) or "")
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
        on_event(
            action_event
        )
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

    async def _ping(self, base_url: str, auth_header: str | None) -> bool:
        headers = self._headers(auth_header)
        async with httpx.AsyncClient(timeout=_DEFAULT_HTTP_TIMEOUT) as client:
            for path in ("/health", "/api/health", "/"):
                try:
                    response = await client.get(f"{base_url}{path}", headers=headers)
                    if response.status_code < 500:
                        return True
                except httpx.HTTPError:
                    continue
        return False

    def _start_server(self) -> None:
        command = self.opencode_start_command or ["opencode", "start"]
        fallback = ["npx", "-y", "@opencode-ai/cli", "start"]
        try:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            logger.info("Started OpenCode daemon with %s", command)
        except FileNotFoundError:
            subprocess.Popen(
                fallback,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
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
                    response = await client.request(method, url, headers=headers, json=body)
                    response.raise_for_status()
                    session_id = _extract_session_id(response.json())
                    if session_id:
                        return session_id
                except httpx.HTTPError:
                    continue
        raise RuntimeError("Failed to create OpenCode session")

    async def _post_prompt(
        self, client: httpx.AsyncClient, session_id: str, prompt_text: str
    ) -> Any:
        assert self._base_url is not None
        headers = self._headers(self._auth_header)
        payload = {"prompt": [{"type": "text", "text": prompt_text}]}
        endpoints = [
            f"{self._base_url}/api/session/{session_id}/prompt",
            f"{self._base_url}/session/{session_id}/prompt",
        ]
        for url in endpoints:
            try:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                return response.json() if response.content else {}
            except httpx.HTTPError:
                continue
        raise RuntimeError("Failed to send prompt to OpenCode")

    async def _wait_for_turn(self, client: httpx.AsyncClient, session_id: str) -> Any:
        assert self._base_url is not None
        headers = self._headers(self._auth_header)
        endpoints = [
            f"{self._base_url}/api/session/{session_id}/wait",
            f"{self._base_url}/session/{session_id}/wait",
        ]
        for url in endpoints:
            try:
                response = await client.post(url, headers=headers, json={})
                response.raise_for_status()
                return response.json() if response.content else {}
            except httpx.HTTPError:
                continue
        return {}

    async def _stream_events(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        on_event: ConversationCallbackType,
        on_token: ConversationTokenCallbackType | None,
        state: ConversationState,
        stream_text: list[str],
        stream_reasoning: list[str],
        pending_tools: dict[str, dict[str, Any]],
    ) -> None:
        assert self._base_url is not None
        headers = self._headers(self._auth_header)
        endpoints = [f"{self._base_url}/api/event", f"{self._base_url}/event"]
        for url in endpoints:
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    async for event_name, data in self._iter_sse(response):
                        if self._extract_event_session_id(data) not in (None, session_id):
                            continue
                        self._handle_stream_event(
                            event_name,
                            data,
                            on_event,
                            on_token,
                            state,
                            stream_text,
                            stream_reasoning,
                            pending_tools,
                        )
                return
            except httpx.HTTPError:
                continue

    async def _iter_sse(
        self, response: httpx.Response
    ) -> AsyncIterator[tuple[str | None, Any]]:
        event_name: str | None = None
        data_lines: list[str] = []
        async for raw_line in response.aiter_lines():
            if raw_line == "":
                if data_lines:
                    payload = "\n".join(data_lines)
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        data = payload
                    yield event_name, data
                event_name = None
                data_lines = []
                continue
            if raw_line.startswith(":"):
                continue
            if raw_line.startswith("event:"):
                event_name = raw_line.split(":", 1)[1].strip()
                continue
            if raw_line.startswith("data:"):
                data_lines.append(raw_line.split(":", 1)[1].lstrip())

    def _handle_stream_event(
        self,
        event_name: str | None,
        data: Any,
        on_event: ConversationCallbackType,
        on_token: ConversationTokenCallbackType | None,
        state: ConversationState,
        stream_text: list[str],
        stream_reasoning: list[str],
        pending_tools: dict[str, dict[str, Any]],
    ) -> None:
        self._signal_activity()
        event_name = event_name or ""
        mask = state.secret_registry.mask_secrets_in_output

        if event_name == "session.next.text.delta":
            text = mask(_extract_text(data) or "")
            if text:
                stream_text.append(text)
                if on_token is not None:
                    on_token(text)
            return

        if event_name == "session.next.reasoning.delta":
            text = mask(_extract_text(data) or "")
            if text:
                stream_reasoning.append(text)
                if on_token is not None:
                    on_token(
                        ModelResponseStream(
                            choices=[
                                StreamingChoices(
                                    index=0,
                                    delta=Delta(content=None, reasoning_content=text),
                                )
                            ]
                        )
                    )
            return

        if event_name in {
            "session.next.tool.called",
            "session.next.tool.progress",
            "session.next.tool.success",
            "session.next.tool.failed",
        }:
            event = self._tool_event_from_payload(event_name, data, pending_tools)
            if event is not None:
                on_event(event)

    def _tool_event_from_payload(
        self,
        event_name: str,
        data: Any,
        pending_tools: dict[str, dict[str, Any]],
    ) -> ACPToolCallEvent | None:
        payload = data if isinstance(data, dict) else {}
        call_id = _first_string(payload, "tool_call_id", "toolCallId", "callID", "id")
        if not call_id:
            return None
        current = dict(pending_tools.get(call_id, {}))
        current.update(payload)
        pending_tools[call_id] = current

        status_map = {
            "session.next.tool.called": "pending",
            "session.next.tool.progress": "in_progress",
            "session.next.tool.success": "completed",
            "session.next.tool.failed": "failed",
        }
        status = status_map[event_name]
        title = _first_string(current, "title", "tool", "name") or "tool"
        raw_input = current.get("input") or current.get("raw_input") or current.get("rawInput")
        raw_output = current.get("output") or current.get("raw_output") or current.get("rawOutput")
        content = current.get("content")
        return ACPToolCallEvent(
            tool_call_id=call_id,
            title=title,
            status=status,
            tool_kind=_first_string(current, "tool_kind", "toolKind", "tool", "name"),
            raw_input=raw_input,
            raw_output=raw_output,
            content=content if isinstance(content, list) else None,
            is_error=status == "failed",
        )

    def _extract_event_session_id(self, data: Any) -> str | None:
        if not isinstance(data, dict):
            return None
        for key in ("session", "payload", "data"):
            nested = data.get(key)
            session_id = _extract_session_ref(nested)
            if session_id:
                return session_id
        return _extract_session_ref(data)

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

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._executor is not None:
            self._executor.close()
            self._executor = None
