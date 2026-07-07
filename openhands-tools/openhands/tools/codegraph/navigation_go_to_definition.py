"""CodeGraph go_to_definition tool — wraps ``codegraph node``."""

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


class GoToDefinitionAction(Action):
    """Schema for resolving a symbol definition."""

    symbol: str = Field(
        description="Name of the function, method, class, or type to locate."
    )
    file: str | None = Field(
        default=None,
        description="Optional file path or suffix to disambiguate overloaded symbols.",
    )
    cwd: str | None = Field(
        default=None,
        description="Optional project root. Defaults to the agent workspace working directory.",
    )


class GoToDefinitionObservation(Observation):
    """Observation from go_to_definition operations."""

    symbol: str = Field(description="The symbol that was resolved")
    search_path: str = Field(description="The project directory that was searched")


TOOL_DESCRIPTION = """Jump to a symbol's definition via CodeGraph (``go_to_definition``).

* Wraps ``codegraph node`` — returns location, signature, and source when available
* Use when you know the symbol name and need its definition (not a broad architecture question)
* Pass ``file`` when the symbol name is overloaded across the codebase
* For broad exploration or call flows, prefer ``codegraph_explore`` instead
* Requires a ``.codegraph/`` index in the project
"""


class GoToDefinitionExecutor(
    ToolExecutor[GoToDefinitionAction, GoToDefinitionObservation]
):
    def __init__(self, working_dir: str):
        self.working_dir = Path(working_dir).resolve()

    def __call__(
        self,
        action: GoToDefinitionAction,
        conversation: LocalConversation | None = None,  # noqa: ARG002
    ) -> GoToDefinitionObservation:
        symbol = action.symbol.strip()
        if not symbol:
            return GoToDefinitionObservation.from_text(
                text="Symbol must not be empty.",
                symbol=symbol,
                search_path=str(self.working_dir),
                is_error=True,
            )

        search_path, path_error = resolve_search_path(self.working_dir, action.cwd)
        if path_error or search_path is None:
            return GoToDefinitionObservation.from_text(
                text=path_error or "Invalid search path.",
                symbol=symbol,
                search_path=str(self.working_dir),
                is_error=True,
            )

        binary, prereq_error = validate_codegraph_prerequisites(search_path)
        if prereq_error or binary is None:
            return GoToDefinitionObservation.from_text(
                text=prereq_error or "CodeGraph is not available.",
                symbol=symbol,
                search_path=str(search_path),
                is_error=True,
            )

        command = [binary, "node", symbol]
        if action.file:
            command.extend(["-f", action.file.strip()])
        command = append_path_flag(command, search_path, self.working_dir)

        result = run_codegraph_cli(
            command=command,
            search_path=search_path,
            timeout_sec=get_codegraph_timeout_sec(),
            error_prefix="CodeGraph node",
        )
        return GoToDefinitionObservation.from_text(
            text=result.text,
            symbol=symbol,
            search_path=str(search_path),
            is_error=result.is_error,
        )


class GoToDefinitionTool(
    ToolDefinition[GoToDefinitionAction, GoToDefinitionObservation]
):
    @classmethod
    def create(cls, conv_state: ConversationState) -> Sequence[GoToDefinitionTool]:
        working_dir = conv_state.workspace.working_dir
        if not os.path.isdir(working_dir):
            raise ValueError(f"working_dir '{working_dir}' is not a valid directory")

        executor = GoToDefinitionExecutor(working_dir=working_dir)
        enhanced_description = (
            f"{TOOL_DESCRIPTION}\n\nYour current working directory is: {working_dir}"
        )
        return [
            cls(
                description=enhanced_description,
                action_type=GoToDefinitionAction,
                observation_type=GoToDefinitionObservation,
                annotations=ToolAnnotations(
                    title="go_to_definition",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=executor,
            )
        ]

    def declared_resources(self, action: Action) -> DeclaredResources:
        if not isinstance(action, GoToDefinitionAction):
            raise TypeError(
                f"Expected GoToDefinitionAction, got {type(action).__name__}"
            )
        return DeclaredResources(keys=(), declared=True)


register_tool(GoToDefinitionTool.name, GoToDefinitionTool)
