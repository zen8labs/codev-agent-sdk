"""CodeGraph list_callers tool — wraps ``codegraph callers``."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field

from openhands.sdk.tool import (
    Action,
    DeclaredResources,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)
from openhands.tools.codegraph.config import get_codegraph_timeout_sec
from openhands.tools.codegraph.navigation_common import (
    append_path_flag,
    resolve_search_path,
    validate_codegraph_prerequisites,
)
from openhands.tools.codegraph.runner import run_codegraph_cli


if TYPE_CHECKING:
    from openhands.sdk.conversation import LocalConversation
    from openhands.sdk.conversation.state import ConversationState


class ListCallersAction(Action):
    """Schema for listing callers of a symbol."""

    symbol: str = Field(description="Name of the function, method, or class to inspect.")
    limit: int = Field(default=20, description="Maximum number of callers to return.")
    cwd: str | None = Field(
        default=None,
        description="Optional project root. Defaults to the agent workspace working directory.",
    )


class ListCallersObservation(Observation):
    """Observation from list_callers operations."""

    symbol: str = Field(description="The symbol that was inspected")
    search_path: str = Field(description="The project directory that was searched")


TOOL_DESCRIPTION = """List functions/methods that call a symbol (``list_callers``).

* Wraps ``codegraph callers`` — precise call sites only
* Use when you need **who calls** a specific function or method
* For all usages (broader, approximate), use ``find_references``; for architecture, use ``codegraph_explore``
* Requires a ``.codegraph/`` index in the project
"""


class ListCallersExecutor(ToolExecutor[ListCallersAction, ListCallersObservation]):
    def __init__(self, working_dir: str):
        self.working_dir = Path(working_dir).resolve()

    def __call__(
        self,
        action: ListCallersAction,
        conversation: LocalConversation | None = None,  # noqa: ARG002
    ) -> ListCallersObservation:
        symbol = action.symbol.strip()
        if not symbol:
            return ListCallersObservation.from_text(
                text="Symbol must not be empty.",
                symbol=symbol,
                search_path=str(self.working_dir),
                is_error=True,
            )

        search_path, path_error = resolve_search_path(self.working_dir, action.cwd)
        if path_error or search_path is None:
            return ListCallersObservation.from_text(
                text=path_error or "Invalid search path.",
                symbol=symbol,
                search_path=str(self.working_dir),
                is_error=True,
            )

        binary, prereq_error = validate_codegraph_prerequisites(search_path)
        if prereq_error or binary is None:
            return ListCallersObservation.from_text(
                text=prereq_error or "CodeGraph is not available.",
                symbol=symbol,
                search_path=str(search_path),
                is_error=True,
            )

        limit = max(1, action.limit)
        command = append_path_flag(
            [binary, "callers", symbol, "-l", str(limit)],
            search_path,
            self.working_dir,
        )
        result = run_codegraph_cli(
            command=command,
            search_path=search_path,
            timeout_sec=get_codegraph_timeout_sec(),
            error_prefix="CodeGraph callers",
        )
        return ListCallersObservation.from_text(
            text=result.text,
            symbol=symbol,
            search_path=str(search_path),
            is_error=result.is_error,
        )


class ListCallersTool(ToolDefinition[ListCallersAction, ListCallersObservation]):
    @classmethod
    def create(cls, conv_state: ConversationState) -> Sequence[ListCallersTool]:
        working_dir = conv_state.workspace.working_dir
        if not os.path.isdir(working_dir):
            raise ValueError(f"working_dir '{working_dir}' is not a valid directory")

        executor = ListCallersExecutor(working_dir=working_dir)
        enhanced_description = (
            f"{TOOL_DESCRIPTION}\n\n"
            f"Your current working directory is: {working_dir}"
        )
        return [
            cls(
                description=enhanced_description,
                action_type=ListCallersAction,
                observation_type=ListCallersObservation,
                annotations=ToolAnnotations(
                    title="list_callers",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=executor,
            )
        ]

    def declared_resources(self, action: Action) -> DeclaredResources:
        if not isinstance(action, ListCallersAction):
            raise TypeError(
                f"Expected ListCallersAction, got {type(action).__name__}"
            )
        return DeclaredResources(keys=(), declared=True)


register_tool(ListCallersTool.name, ListCallersTool)
