"""Git router for OpenHands SDK."""

import asyncio
import functools
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from openhands.agent_server.server_details_router import update_last_execution_time
from openhands.sdk.git.exceptions import GitError, GitRepositoryError
from openhands.sdk.git.git_changes import get_git_changes
from openhands.sdk.git.git_diff import get_git_diff
from openhands.sdk.git.models import GitChange, GitDiff


git_router = APIRouter(prefix="/git", tags=["Git"])
logger = logging.getLogger(__name__)


_REF_QUERY_DESCRIPTION = (
    "Optional git ref to diff against (e.g. 'HEAD' for git status-style "
    "changes, or a commit hash). When omitted, the upstream/default branch "
    "is auto-detected."
)

# The GUI builds git paths assuming the Docker sandbox mount layout, where the
# workspace is always mounted at ``/workspace/project[/<conversation-id>]``. In
# non-Docker sandboxes (notably the local *process* sandbox used for dev and the
# CLI) the agent-server runs with its working directory set to the real
# workspace root, which is some host path like
# ``/var/folders/.../openhands-sandboxes/<id>`` — so the GUI's hard-coded
# ``/workspace/project`` prefix points at a directory that does not exist and
# every Git lookup silently returns "no changes". ``_resolve_workspace_path``
# bridges that gap by re-rooting such a path under the server's actual workspace.
_ASSUMED_WORKSPACE_PREFIX = Path("/workspace/project")


def _is_conversation_id_segment(segment: str) -> bool:
    """True for a 32-char hex segment (a UUID ``.hex``, as used by sandbox
    grouping to give each conversation its own subdirectory)."""
    return len(segment) == 32 and all(c in "0123456789abcdef" for c in segment.lower())


def _resolve_workspace_path(path: str) -> str:
    """Map a GUI-supplied git path onto the server's real workspace root.

    No-op when ``path`` already exists (the Docker case, and any absolute path
    that happens to be correct), so this never changes behavior where the GUI's
    assumption holds. Only when the path is missing do we try to re-root the
    trailing repo component(s) — i.e. the part after
    ``/workspace/project[/<conversation-id>]`` — under the server's working
    directory. If that re-rooted path does not exist either, the original is
    returned unchanged so the caller's existing not-a-repo handling still runs.
    """
    if Path(path).exists():
        return path
    try:
        rel = Path(path).relative_to(_ASSUMED_WORKSPACE_PREFIX)
    except ValueError:
        # Not the assumed GUI layout (e.g. an already-correct host path that is
        # merely missing); leave it untouched.
        return path

    parts = rel.parts
    if parts and _is_conversation_id_segment(parts[0]):
        parts = parts[1:]

    candidate = Path.cwd().joinpath(*parts)
    if candidate.exists():
        logger.debug("Re-rooted workspace path %s -> %s", path, candidate)
        return str(candidate)
    return path


async def _get_git_changes(path: str, ref: str | None) -> list[GitChange]:
    """Internal helper to get git changes for a given path."""
    update_last_execution_time()
    path = _resolve_workspace_path(path)
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, functools.partial(get_git_changes, Path(path), ref=ref)
        )
    except GitRepositoryError:
        # A non-repo workspace has no git changes to report; respond with an
        # empty list so the Changes tab can render normally instead of 500ing.
        logger.debug("Path %s is not a git repository; returning no changes", path)
        return []


async def _get_git_diff(path: str, ref: str | None) -> GitDiff:
    """Internal helper to get git diff for a given path."""
    update_last_execution_time()
    path = _resolve_workspace_path(path)
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, functools.partial(get_git_diff, Path(path), ref=ref)
        )
    except GitRepositoryError:
        # Only collapse the not-a-repo case to an empty diff; file-level
        # GitPathError (missing/oversize/outside-repo) stays a 500 so
        # callers can distinguish it from "no changes".
        logger.debug("Path %s is not in a git repository; returning empty diff", path)
        return GitDiff(modified=None, original=None)


@git_router.get("/changes")
async def git_changes_query(
    path: str = Query(..., description="The git repository path"),
    ref: str | None = Query(None, description=_REF_QUERY_DESCRIPTION),
) -> list[GitChange]:
    """Get git changes using query parameter (preferred method)."""
    try:
        return await _get_git_changes(path, ref)
    except GitError as e:
        # GitRepositoryError is already handled in the helper (returns []).
        # Any remaining GitError subclass (e.g. GitCommandError) surfaces as
        # 400 so the client can show an actionable error instead of an
        # opaque 500.
        raise HTTPException(status_code=400, detail=str(e))


@git_router.get("/diff")
async def git_diff_query(
    path: str = Query(..., description="The file path to get diff for"),
    ref: str | None = Query(None, description=_REF_QUERY_DESCRIPTION),
) -> GitDiff:
    """Get git diff using query parameter (preferred method)."""
    try:
        return await _get_git_diff(path, ref)
    except GitError as e:
        # GitRepositoryError is already handled in the helper (returns an
        # empty diff). Any remaining GitError subclass (e.g. GitCommandError,
        # GitPathError) surfaces as 400 so the client can show an actionable
        # error instead of an opaque 500.
        raise HTTPException(status_code=400, detail=str(e))
