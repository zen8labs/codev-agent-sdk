"""Bootstrap helpers for building CodeGraph indexes in remote workspaces."""

from __future__ import annotations

import os
import shlex
from collections.abc import Awaitable, Callable
from typing import Protocol

from openhands.sdk.logger import get_logger
from openhands.tools.codegraph.config import (
    CODEGRAPH_INDEX_DIR,
    get_codegraph_bin,
    get_init_timeout_sec,
    should_init_on_start,
)


logger = get_logger(__name__)


class CommandResult(Protocol):
    exit_code: int
    stderr: str
    stdout: str


ExecuteCommand = Callable[..., Awaitable[CommandResult]]


async def _sandbox_path_is_dir(
    path: str,
    execute_command: ExecuteCommand,
) -> bool:
    """Return True when ``path`` exists as a directory inside the sandbox."""
    result = await execute_command(
        f"test -d {shlex.quote(path)}",
        timeout=10.0,
    )
    return result.exit_code == 0


async def _sandbox_cli_available(
    binary: str,
    execute_command: ExecuteCommand,
) -> bool:
    """Return True when the CodeGraph CLI is executable inside the sandbox."""
    result = await execute_command(f"{binary} --version", timeout=30.0)
    return result.exit_code == 0


async def ensure_codegraph_index(
    project_dir: str,
    execute_command: ExecuteCommand,
) -> bool:
    """Build a CodeGraph index when enabled and missing.

    All filesystem and CLI checks run inside the sandbox via ``execute_command``.
    The app server host does not need the CodeGraph binary installed locally.

    Args:
        project_dir: Repository root inside the sandbox workspace.
        execute_command: Async callable with signature
            ``(command, cwd=None, timeout=30.0)``.

    Returns:
        True when an index exists after this call (or already existed), False otherwise.
    """
    index_dir = f"{project_dir.rstrip('/')}/{CODEGRAPH_INDEX_DIR}"

    if not should_init_on_start():
        return await _sandbox_path_is_dir(index_dir, execute_command)

    if await _sandbox_path_is_dir(index_dir, execute_command):
        logger.debug("CodeGraph index already present at %s", project_dir)
        return True

    binary = get_codegraph_bin()
    if not await _sandbox_cli_available(binary, execute_command):
        logger.warning(
            "CodeGraph init skipped: CLI not found in sandbox "
            "(set CODEGRAPH_BIN or install codegraph in the agent-server image)"
        )
        return False

    timeout = float(get_init_timeout_sec())
    logger.info("Initializing CodeGraph index in %s", project_dir)
    result = await execute_command(f"{binary} init", cwd=project_dir, timeout=timeout)
    if result.exit_code:
        logger.warning(
            "CodeGraph init failed in %s (exit %s): %s",
            project_dir,
            result.exit_code,
            (result.stderr or result.stdout or "").strip(),
        )

    return await _sandbox_path_is_dir(index_dir, execute_command)


def get_sandbox_env_for_codegraph() -> dict[str, str]:
    """Return CodeGraph-related env vars to forward into sandbox containers."""
    keys = (
        "OH_ENABLE_CODEGRAPH",
        "CODEGRAPH_BIN",
        "CODEGRAPH_INIT_ON_START",
        "CODEGRAPH_TIMEOUT_SEC",
        "CODEGRAPH_INIT_TIMEOUT_SEC",
    )
    return {key: value for key in keys if (value := os.getenv(key))}
