"""Shared helpers for CodeGraph navigation tools."""

from __future__ import annotations

from pathlib import Path

from openhands.tools.codegraph.config import has_codegraph_index, resolve_codegraph_bin


def resolve_search_path(
    working_dir: Path, cwd: str | None
) -> tuple[Path | None, str | None]:
    """Resolve the project search path, returning an error message on failure."""
    if cwd:
        search_path = Path(cwd).resolve()
        if not search_path.is_dir():
            return None, f"Search path '{cwd}' is not a valid directory."
        return search_path, None
    return working_dir, None


def validate_codegraph_prerequisites(
    search_path: Path,
) -> tuple[str | None, str | None]:
    """Return ``(binary_path, error_message)``."""
    binary = resolve_codegraph_bin()
    if binary is None:
        return None, (
            "CodeGraph CLI is not installed or not on PATH. "
            "Install it with the CodeGraph installer or set CODEGRAPH_BIN."
        )

    if not has_codegraph_index(search_path):
        return None, (
            f"No CodeGraph index found at '{search_path / '.codegraph'}'. "
            "Run `codegraph init` in the project root before using CodeGraph tools."
        )

    return binary, None


def append_path_flag(
    command: list[str], search_path: Path, working_dir: Path
) -> list[str]:
    """Add ``-p`` when the search path differs from the executor working dir."""
    if search_path != working_dir:
        return [*command, "-p", str(search_path)]
    return command
