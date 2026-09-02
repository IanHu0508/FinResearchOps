"""Runtime enforcement for the workspace's private-storage seam."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class PrivateStorageError(ValueError):
    """A path would place confidential runtime material outside private/."""


@dataclass(frozen=True, slots=True)
class PrivateWorkspaceAnchor:
    """Resolved identity of one sibling public/private workspace boundary."""

    workspace_root: Path
    private_root: Path
    public_worktree: Path


def resolve_private_workspace_anchor(
    path: Path,
    *,
    purpose: str,
) -> PrivateWorkspaceAnchor:
    """Resolve the one workspace anchor authorized by a private root path."""

    _, anchor = _resolve_private_path(path, purpose=purpose)
    return anchor


def require_private_storage_root(
    path: Path,
    *,
    purpose: str,
    anchor: PrivateWorkspaceAnchor | None = None,
) -> Path:
    """Return a resolved private root or fail before any confidential write.

    The authorized workspace has one public Git worktree, ``finaudit-gate/``,
    and one sibling ``private/`` tree.  Confidential roots must be absolute,
    must resolve below that sibling, and may not cross any symbolic link.
    """

    resolved_path, detected_anchor = _resolve_private_path(path, purpose=purpose)
    if anchor is not None:
        if type(anchor) is not PrivateWorkspaceAnchor:
            raise TypeError("anchor must be a PrivateWorkspaceAnchor")
        if detected_anchor != anchor:
            raise PrivateStorageError(
                f"PRIVATE_STORAGE_REQUIRED:{purpose}:WORKSPACE_ANCHOR_MISMATCH"
            )
    return resolved_path


def _resolve_private_path(
    path: Path,
    *,
    purpose: str,
) -> tuple[Path, PrivateWorkspaceAnchor]:
    if not isinstance(path, Path):
        raise TypeError(f"{purpose} must be a pathlib.Path")
    if type(purpose) is not str or not purpose:
        raise ValueError("purpose must be a non-empty string")
    if not path.is_absolute() or ".." in path.parts:
        raise PrivateStorageError(
            f"PRIVATE_STORAGE_REQUIRED:{purpose}:ABSOLUTE_PATH_REQUIRED"
        )

    private_root = next(
        (candidate for candidate in (path, *path.parents) if candidate.name == "private"),
        None,
    )
    if private_root is None:
        raise PrivateStorageError(
            f"PRIVATE_STORAGE_REQUIRED:{purpose}:PRIVATE_ANCESTOR_MISSING"
        )
    public_worktree = private_root.parent / "finaudit-gate"
    git_marker = public_worktree / ".git"
    if not git_marker.exists() or public_worktree.is_symlink():
        raise PrivateStorageError(
            f"PRIVATE_STORAGE_REQUIRED:{purpose}:WORKSPACE_SEAM_MISSING"
        )
    if private_root.is_symlink():
        raise PrivateStorageError(
            f"PRIVATE_STORAGE_REQUIRED:{purpose}:SYMLINK_FORBIDDEN"
        )

    resolved_private = private_root.resolve(strict=True)
    resolved_public = public_worktree.resolve(strict=True)
    resolved_workspace = resolved_private.parent
    resolved_path = path.resolve(strict=False)
    if resolved_path == resolved_private or not resolved_path.is_relative_to(
        resolved_private
    ):
        raise PrivateStorageError(
            f"PRIVATE_STORAGE_REQUIRED:{purpose}:PRIVATE_ESCAPE"
        )
    if resolved_path == resolved_public or resolved_path.is_relative_to(
        resolved_public
    ):
        raise PrivateStorageError(
            f"PRIVATE_STORAGE_REQUIRED:{purpose}:PUBLIC_WORKTREE_FORBIDDEN"
        )

    current = private_root
    relative_parts = path.relative_to(private_root).parts
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            raise PrivateStorageError(
                f"PRIVATE_STORAGE_REQUIRED:{purpose}:SYMLINK_FORBIDDEN"
            )
    return resolved_path, PrivateWorkspaceAnchor(
        workspace_root=resolved_workspace,
        private_root=resolved_private,
        public_worktree=resolved_public,
    )
