"""CodeGraph explore executor — wraps the CodeGraph CLI."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from openhands.sdk.logger import get_logger
from openhands.sdk.tool import ToolExecutor
from openhands.sdk.utils import sanitized_env
from openhands.tools.codegraph.config import (
    get_explore_timeout_sec,
    has_codegraph_index,
    resolve_codegraph_bin,
)
from openhands.tools.codegraph.definition import (
    CodeGraphExploreAction,
    CodeGraphExploreObservation,
)


if TYPE_CHECKING:
    from openhands.sdk.conversation import LocalConversation

logger = get_logger(__name__)


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

        if action.cwd:
            search_path = Path(action.cwd).resolve()
            if not search_path.is_dir():
                return CodeGraphExploreObservation.from_text(
                    text=f"Search path '{action.cwd}' is not a valid directory.",
                    query=query,
                    search_path=str(search_path),
                    is_error=True,
                )
        else:
            search_path = self.working_dir

        binary = resolve_codegraph_bin()
        if binary is None:
            return CodeGraphExploreObservation.from_text(
                text=(
                    "CodeGraph CLI is not installed or not on PATH. "
                    "Install it with the CodeGraph installer or set CODEGRAPH_BIN."
                ),
                query=query,
                search_path=str(search_path),
                is_error=True,
            )

        if not has_codegraph_index(search_path):
            return CodeGraphExploreObservation.from_text(
                text=(
                    f"No CodeGraph index found at '{search_path / '.codegraph'}'. "
                    "Run `codegraph init` in the project root before exploring."
                ),
                query=query,
                search_path=str(search_path),
                is_error=True,
            )

        command = [binary, "explore", query]
        timeout = get_explore_timeout_sec()
        try:
            completed = subprocess.run(
                command,
                cwd=str(search_path),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=sanitized_env(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CodeGraphExploreObservation.from_text(
                text=(
                    f"CodeGraph explore timed out after {timeout} seconds. "
                    "Try a narrower query or increase CODEGRAPH_TIMEOUT_SEC."
                ),
                query=query,
                search_path=str(search_path),
                is_error=True,
            )
        except OSError as exc:
            logger.warning("CodeGraph explore failed to start: %s", exc)
            return CodeGraphExploreObservation.from_text(
                text=f"Failed to run CodeGraph CLI: {exc}",
                query=query,
                search_path=str(search_path),
                is_error=True,
            )

        output = (completed.stdout or "").strip()
        if completed.stderr:
            stderr = completed.stderr.strip()
            if output:
                output = f"{output}\n\n[stderr]\n{stderr}"
            else:
                output = stderr

        if completed.returncode != 0:
            message = output or (
                f"CodeGraph explore failed with exit code {completed.returncode}."
            )
            return CodeGraphExploreObservation.from_text(
                text=message,
                query=query,
                search_path=str(search_path),
                is_error=True,
            )

        if not output:
            output = "CodeGraph explore completed with no output."

        return CodeGraphExploreObservation.from_text(
            text=output,
            query=query,
            search_path=str(search_path),
            is_error=False,
        )
