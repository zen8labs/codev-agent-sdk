"""CodeGraph find_references tool — temporary multi-CLI workaround."""

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
from openhands.tools.codegraph.runner import (
    CodeGraphRunResult,
    run_codegraph_cli_batch,
)


if TYPE_CHECKING:
    from openhands.sdk.conversation import LocalConversation
    from openhands.sdk.conversation.state import ConversationState


FIND_REFERENCES_DISCLAIMER = (
    "> NOTE: Temporary multi-CLI implementation. Does not cover all LSP reference kinds.\n"
    "> Prefer ``list_callers`` for precise call sites. Future: ``codegraph references`` CLI."
)

SECTION_CALLERS = "## Call sites (callers)"
SECTION_IMPACT = "## Dependents (impact depth=1)"
SECTION_QUERY = "## Symbol matches (query)"


class FindReferencesAction(Action):
    """Schema for approximate reference search across CodeGraph."""

    symbol: str = Field(description="Name of the symbol to find usages for.")
    file: str | None = Field(
        default=None,
        description="Reserved for future disambiguation when native references CLI ships.",
    )
    limit: int = Field(default=20, description="Maximum results per CLI subsection.")
    cwd: str | None = Field(
        default=None,
        description="Optional project root. Defaults to the agent workspace working directory.",
    )


class FindReferencesObservation(Observation):
    """Observation from find_references operations."""

    symbol: str = Field(description="The symbol that was inspected")
    search_path: str = Field(description="The project directory that was searched")


TOOL_DESCRIPTION = """Find usages of a symbol via CodeGraph (``find_references``) — **approximate**.

* **Temporary implementation:** runs ``callers``, ``impact --depth 1``, and ``query``, then merges output
* Does **not** match IDE/LSP find-references (misses many type/import-only usages)
* Use ``list_callers`` when you only need direct call sites
* The ``Symbol matches (query)`` section lists name matches — **not** reference sites
* For broad exploration, prefer ``codegraph_explore``
* Requires a ``.codegraph/`` index in the project
"""


def _section_text(result: CodeGraphRunResult, empty_label: str) -> str:
    if result.is_error:
        return f"_{result.text}_"
    if result.text.strip():
        return result.text
    return f"_{empty_label}_"


def merge_find_references_output(symbol: str, results: list[CodeGraphRunResult]) -> str:
    """Merge multi-CLI results into a single observation string."""
    callers, impact, query = results
    sections = [
        f"# find_references: {symbol}",
        FIND_REFERENCES_DISCLAIMER,
        SECTION_CALLERS,
        _section_text(callers, "No call sites found."),
        SECTION_IMPACT,
        _section_text(impact, "No dependents found."),
        SECTION_QUERY,
        _section_text(
            query,
            "No symbol matches found. (This section is for disambiguation, not usages.)",
        ),
    ]
    return "\n\n".join(sections)


class FindReferencesExecutor(ToolExecutor[FindReferencesAction, FindReferencesObservation]):
    def __init__(self, working_dir: str):
        self.working_dir = Path(working_dir).resolve()

    def __call__(
        self,
        action: FindReferencesAction,
        conversation: LocalConversation | None = None,  # noqa: ARG002
    ) -> FindReferencesObservation:
        symbol = action.symbol.strip()
        if not symbol:
            return FindReferencesObservation.from_text(
                text="Symbol must not be empty.",
                symbol=symbol,
                search_path=str(self.working_dir),
                is_error=True,
            )

        search_path, path_error = resolve_search_path(self.working_dir, action.cwd)
        if path_error or search_path is None:
            return FindReferencesObservation.from_text(
                text=path_error or "Invalid search path.",
                symbol=symbol,
                search_path=str(self.working_dir),
                is_error=True,
            )

        binary, prereq_error = validate_codegraph_prerequisites(search_path)
        if prereq_error or binary is None:
            return FindReferencesObservation.from_text(
                text=prereq_error or "CodeGraph is not available.",
                symbol=symbol,
                search_path=str(search_path),
                is_error=True,
            )

        limit = max(1, action.limit)
        timeout_sec = get_codegraph_timeout_sec()
        commands = [
            append_path_flag(
                [binary, "callers", symbol, "-l", str(limit)],
                search_path,
                self.working_dir,
            ),
            append_path_flag(
                [binary, "impact", symbol, "--depth", "1"],
                search_path,
                self.working_dir,
            ),
            append_path_flag(
                [binary, "query", symbol, "-l", str(limit)],
                search_path,
                self.working_dir,
            ),
        ]
        results = run_codegraph_cli_batch(
            commands=commands,
            search_path=search_path,
            timeout_sec=timeout_sec,
            error_prefixes=[
                "CodeGraph callers",
                "CodeGraph impact",
                "CodeGraph query",
            ],
        )

        merged = merge_find_references_output(symbol, results)
        all_failed = all(result.is_error for result in results)
        return FindReferencesObservation.from_text(
            text=merged,
            symbol=symbol,
            search_path=str(search_path),
            is_error=all_failed,
        )


class FindReferencesTool(ToolDefinition[FindReferencesAction, FindReferencesObservation]):
    @classmethod
    def create(cls, conv_state: ConversationState) -> Sequence[FindReferencesTool]:
        working_dir = conv_state.workspace.working_dir
        if not os.path.isdir(working_dir):
            raise ValueError(f"working_dir '{working_dir}' is not a valid directory")

        executor = FindReferencesExecutor(working_dir=working_dir)
        enhanced_description = (
            f"{TOOL_DESCRIPTION}\n\n"
            f"Your current working directory is: {working_dir}"
        )
        return [
            cls(
                description=enhanced_description,
                action_type=FindReferencesAction,
                observation_type=FindReferencesObservation,
                annotations=ToolAnnotations(
                    title="find_references",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=executor,
            )
        ]

    def declared_resources(self, action: Action) -> DeclaredResources:
        if not isinstance(action, FindReferencesAction):
            raise TypeError(
                f"Expected FindReferencesAction, got {type(action).__name__}"
            )
        return DeclaredResources(keys=(), declared=True)


register_tool(FindReferencesTool.name, FindReferencesTool)
