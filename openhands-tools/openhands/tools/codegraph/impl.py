"""CodeGraph explore executor — wraps the CodeGraph CLI."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from openhands.sdk.tool import ToolExecutor
from openhands.tools.codegraph.config import get_codegraph_timeout_sec
from openhands.tools.codegraph.definition import (
    CodeGraphExploreAction,
    CodeGraphExploreObservation,
)
from openhands.tools.codegraph.navigation_common import (
    resolve_search_path,
    validate_codegraph_prerequisites,
)
from openhands.tools.codegraph.runner import run_codegraph_cli


if TYPE_CHECKING:
    from openhands.sdk.conversation import LocalConversation


class CodeGraphExploreExecutor(ToolExecutor[CodeGraphExploreAction, CodeGraphExploreObservation]):
    """Execute ``codegraph explore`` against a pre-built project index."""

    def __init__(self, working_dir: str):
        self.working_dir = Path(working_dir).resolve()

    def __call__(
        self,
        action: CodeGraphExploreAction,
        conversation: LocalConversation | None = None,  # noqa: ARG002
    ) -> CodeGraphExploreObservation:
        query = action.query.strip()
        if not query:
            return CodeGraphExploreObservation.from_text(
                text="Query must not be empty.",
                query=query,
                search_path=str(self.working_dir),
                is_error=True,
            )

        search_path, path_error = resolve_search_path(self.working_dir, action.cwd)
        if path_error or search_path is None:
            return CodeGraphExploreObservation.from_text(
                text=path_error or "Invalid search path.",
                query=query,
                search_path=str(self.working_dir),
                is_error=True,
            )

        binary, prereq_error = validate_codegraph_prerequisites(search_path)
        if prereq_error or binary is None:
            return CodeGraphExploreObservation.from_text(
                text=prereq_error or "CodeGraph is not available.",
                query=query,
                search_path=str(search_path),
                is_error=True,
            )

        result = run_codegraph_cli(
            command=[binary, "explore", query],
            search_path=search_path,
            timeout_sec=get_codegraph_timeout_sec(),
            error_prefix="CodeGraph explore",
        )
        return CodeGraphExploreObservation.from_text(
            text=result.text,
            query=query,
            search_path=str(search_path),
            is_error=result.is_error,
        )
