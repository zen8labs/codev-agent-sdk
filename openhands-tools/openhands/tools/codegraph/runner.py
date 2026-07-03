"""Shared CodeGraph CLI execution helpers."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from openhands.sdk.logger import get_logger
from openhands.sdk.utils import sanitized_env


logger = get_logger(__name__)


@dataclass(frozen=True)
class CodeGraphRunResult:
    """Result of a single CodeGraph CLI invocation."""

    text: str
    is_error: bool
    returncode: int | None = None


def format_cli_output(completed: subprocess.CompletedProcess[str]) -> str:
    """Merge stdout and stderr into a single output string."""
    output = (completed.stdout or "").strip()
    if completed.stderr:
        stderr = completed.stderr.strip()
        if output:
            output = f"{output}\n\n[stderr]\n{stderr}"
        else:
            output = stderr
    return output


def run_codegraph_cli(
    *,
    command: list[str],
    search_path: Path,
    timeout_sec: int,
    error_prefix: str,
) -> CodeGraphRunResult:
    """Run a CodeGraph CLI command and return structured output."""
    try:
        completed = subprocess.run(
            command,
            cwd=str(search_path),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=sanitized_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CodeGraphRunResult(
            text=(
                f"{error_prefix} timed out after {timeout_sec} seconds. "
                "Try a narrower query or increase CODEGRAPH_TIMEOUT_SEC."
            ),
            is_error=True,
            returncode=None,
        )
    except OSError as exc:
        logger.warning("%s failed to start: %s", error_prefix, exc)
        return CodeGraphRunResult(
            text=f"Failed to run CodeGraph CLI: {exc}",
            is_error=True,
            returncode=None,
        )

    output = format_cli_output(completed)
    if completed.returncode != 0:
        message = output or (
            f"{error_prefix} failed with exit code {completed.returncode}."
        )
        return CodeGraphRunResult(
            text=message,
            is_error=True,
            returncode=completed.returncode,
        )

    if not output:
        output = f"{error_prefix} completed with no output."

    return CodeGraphRunResult(
        text=output,
        is_error=False,
        returncode=completed.returncode,
    )


def run_codegraph_cli_batch(
    *,
    commands: list[list[str]],
    search_path: Path,
    timeout_sec: int,
    error_prefixes: list[str],
) -> list[CodeGraphRunResult]:
    """Run multiple CodeGraph CLI commands sequentially."""
    if len(error_prefixes) != len(commands):
        raise ValueError("error_prefixes must match commands length")
    return [
        run_codegraph_cli(
            command=command,
            search_path=search_path,
            timeout_sec=timeout_sec,
            error_prefix=prefix,
        )
        for command, prefix in zip(commands, error_prefixes, strict=True)
    ]
