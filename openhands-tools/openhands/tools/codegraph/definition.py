"""CodeGraph explore tool definition."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import TYPE_CHECKING

from pydantic import Field

from openhands.sdk.tool import (
    Action,
    DeclaredResources,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    register_tool,
)


if TYPE_CHECKING:
    from openhands.sdk.conversation.state import ConversationState


class CodeGraphExploreAction(Action):
    """Schema for CodeGraph semantic exploration."""

    query: str = Field(
        description=(
            "Natural-language question about code structure, flows, or symbols. "
            'Examples: "how does authentication work", '
            '"find definition of UserService", "callers of process_request".'
        )
    )
    cwd: str | None = Field(
        default=None,
        description=(
            "Optional project root to search in. "
            "Defaults to the agent workspace working directory."
        ),
    )


class CodeGraphExploreObservation(Observation):
    """Observation from CodeGraph explore operations."""

    query: str = Field(description="The explore query that was executed")
    search_path: str = Field(description="The project directory that was searched")


TOOL_DESCRIPTION = """Semantic code exploration via CodeGraph (tool name: ``codegraph_explore``).

* **Call** ``codegraph_explore`` — not ``codegraph`` (that name is only for ``invoke_skill``)
* **Use first** for structure: symbol relationships, call graphs, flows, blast radius
* **Do not** use terminal grep/find/rg for structural discovery when this tool is available
* Requires a CodeGraph index (``.codegraph/``) in the project — run ``codegraph init`` first if missing
* Returns relevant source snippets, call paths, and blast-radius summaries
* **Fallback to grep/file_editor** only when this tool errors, times out, returns no useful results, or you need literal text search in known files
* If output mentions a stale file, read that file directly for the latest content
"""


class CodegraphExploreTool(ToolDefinition[CodeGraphExploreAction, CodeGraphExploreObservation]):
    """Tool that wraps ``codegraph explore`` for agent use."""

    def declared_resources(self, action: Action) -> DeclaredResources:
        if not isinstance(action, CodeGraphExploreAction):
            raise TypeError(
                f"Expected CodeGraphExploreAction, got {type(action).__name__}"
            )
        return DeclaredResources(keys=(), declared=True)

    @classmethod
    def create(
        cls,
        conv_state: ConversationState,
    ) -> Sequence[CodegraphExploreTool]:
        from openhands.tools.codegraph.impl import CodeGraphExploreExecutor

        working_dir = conv_state.workspace.working_dir
        if not os.path.isdir(working_dir):
            raise ValueError(f"working_dir '{working_dir}' is not a valid directory")

        executor = CodeGraphExploreExecutor(working_dir=working_dir)
        enhanced_description = (
            f"{TOOL_DESCRIPTION}\n\n"
            f"Your current working directory is: {working_dir}\n"
            "Exploration runs against the CodeGraph index in that directory unless "
            "you provide cwd."
        )
        return [
            cls(
                description=enhanced_description,
                action_type=CodeGraphExploreAction,
                observation_type=CodeGraphExploreObservation,
                annotations=ToolAnnotations(
                    title="codegraph_explore",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=executor,
            )
        ]


register_tool(CodegraphExploreTool.name, CodegraphExploreTool)
