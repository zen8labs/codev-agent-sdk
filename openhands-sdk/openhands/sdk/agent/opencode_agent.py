from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
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

        self._signal_activity()
        try:
            async with httpx.AsyncClient(
                timeout=self.opencode_prompt_timeout
            ) as client:
                response_data = await self._post_prompt(client, session_id, prompt_text)
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

        # Parse the response: {"info": {...}, "parts": [...]}
        # The POST /session/{id}/message endpoint returns the complete
        # assistant message with parts for step-start, reasoning, text,
        # and step-finish.
        mask = state.secret_registry.mask_secrets_in_output
        stream_text: list[str] = []
        stream_reasoning: list[str] = []

        # Check for model-level errors in the response info.
        info = response_data.get("info", {}) if isinstance(response_data, dict) else {}
        error_info = info.get("error") if isinstance(info, dict) else None
        if isinstance(error_info, dict):
            # OpenCode nests the human-readable message under
            # ``data.message``; check top-level keys first, then nested.
            error_msg = _first_string(error_info, "message", "error", "detail")
            if not error_msg:
                nested = error_info.get("data")
                if isinstance(nested, dict):
                    error_msg = _first_string(nested, "message", "error", "detail")
            if not error_msg:
                error_msg = str(error_info)
            on_event(
                MessageEvent(
                    source="agent",
                    llm_message=Message(
                        role="assistant",
                        content=[
                            TextContent(text=f"OpenCode model error: {error_msg}")
                        ],
                    ),
                )
            )
            on_event(
                ConversationErrorEvent(
                    source="agent",
                    code="OpenCodeModelError",
                    detail=error_msg[:500],
                )
            )
            state.execution_status = ConversationExecutionStatus.ERROR
            return

        parts = (
            response_data.get("parts", []) if isinstance(response_data, dict) else []
        )
        for part in parts:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type", "")
            if part_type == "text":
                text = mask(part.get("text", ""))
                if text:
                    stream_text.append(text)
                    if on_token is not None:
                        on_token(
                            ModelResponseStream(
                                choices=[
                                    StreamingChoices(
                                        index=0,
                                        delta=Delta(content=text),
                                    )
                                ]
                            )
                        )
            elif part_type == "reasoning":
                text = mask(part.get("text", ""))
                if text:
                    stream_reasoning.append(text)
                    if on_token is not None:
                        on_token(
                            ModelResponseStream(
                                choices=[
                                    StreamingChoices(
                                        index=0,
                                        delta=Delta(
                                            content=None,
                                            reasoning_content=text,
                                        ),
                                    )
                                ]
                            )
                        )

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
        command = self.opencode_start_command or ["opencode", "serve"]
        fallback = ["npx", "-y", "@opencode-ai/cli", "serve"]
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

    async def _post_prompt(
        self, client: httpx.AsyncClient, session_id: str, prompt_text: str
    ) -> Any:
        assert self._base_url is not None
        headers = self._headers(self._auth_header)
        # The OpenCode v1/v2 API accepts a parts array on the
        # /session/{id}/message endpoint, which blocks until the model
        # finishes and returns the complete assistant message.
        payload = {"parts": [{"type": "text", "text": prompt_text}]}
        url = f"{self._base_url}/session/{session_id}/message"
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        try:
            return response.json() if response.content else {}
        except Exception:
            return {}

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
