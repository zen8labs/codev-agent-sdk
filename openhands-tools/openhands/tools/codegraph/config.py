"""Configuration helpers for CodeGraph tool integration."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

CODEGRAPH_INDEX_DIR = ".codegraph"

_DEFAULT_TIMEOUT_SEC = 120
_DEFAULT_INIT_TIMEOUT_SEC = 600


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def is_codegraph_enabled() -> bool:
    """Return True when CodeGraph tools should be registered for agents."""
    return _env_flag("OH_ENABLE_CODEGRAPH", False)


def should_init_on_start() -> bool:
    """Return True when a missing index should be built at conversation start."""
    if not is_codegraph_enabled():
        return False
    return _env_flag("CODEGRAPH_INIT_ON_START", True)


def get_codegraph_bin() -> str:
    """Resolve the CodeGraph CLI binary path."""
    configured = (os.getenv("CODEGRAPH_BIN") or "").strip()
    if configured:
        return configured
    return "codegraph"


def resolve_codegraph_bin() -> str | None:
    """Return an executable CodeGraph binary path, or None if unavailable."""
    configured = get_codegraph_bin()
    path = shutil.which(configured)
    if path:
        return path
    candidate = Path(configured)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate.resolve())
    return None


def get_codegraph_timeout_sec() -> int:
    raw = os.getenv("CODEGRAPH_TIMEOUT_SEC")
    if not raw:
        return _DEFAULT_TIMEOUT_SEC
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_TIMEOUT_SEC


def get_explore_timeout_sec() -> int:
    """Backward-compatible alias for :func:`get_codegraph_timeout_sec`."""
    return get_codegraph_timeout_sec()


def get_init_timeout_sec() -> int:
    raw = os.getenv("CODEGRAPH_INIT_TIMEOUT_SEC")
    if not raw:
        return _DEFAULT_INIT_TIMEOUT_SEC
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_INIT_TIMEOUT_SEC


def has_codegraph_index(project_dir: str | Path) -> bool:
    """Return True when the project has a CodeGraph index directory."""
    return (Path(project_dir) / CODEGRAPH_INDEX_DIR).is_dir()
