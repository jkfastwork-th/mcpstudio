from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GitWorktreeFamily:
    common_dir: Path
    source_worktree: Path
    worktrees: tuple[Path, ...]


def _canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _git_output(cwd: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _resolve_git_path(cwd: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return _canonical(path)


def git_worktree_family(path: Path) -> GitWorktreeFamily | None:
    probe = _canonical(path)
    if not probe.exists():
        probe = probe.parent
    if not probe.is_dir():
        probe = probe.parent

    top = _resolve_git_path(probe, _git_output(probe, "rev-parse", "--show-toplevel"))
    common_dir = _resolve_git_path(probe, _git_output(probe, "rev-parse", "--git-common-dir"))
    porcelain = _git_output(probe, "worktree", "list", "--porcelain")
    if top is None or common_dir is None or porcelain is None:
        return None

    worktrees: list[Path] = []
    for line in porcelain.splitlines():
        if not line.startswith("worktree "):
            continue
        raw = line[len("worktree ") :].strip()
        if raw:
            worktrees.append(_canonical(Path(raw)))

    source_worktree = _canonical(top)
    if source_worktree not in worktrees:
        return None
    return GitWorktreeFamily(
        common_dir=common_dir,
        source_worktree=source_worktree,
        worktrees=tuple(worktrees),
    )


def is_registered_git_worktree_path(workspace_root: Path, candidate: Path) -> bool:
    """Return True when candidate is inside a registered worktree of workspace_root's repo."""
    family = git_worktree_family(workspace_root)
    if family is None:
        return False

    target = _canonical(candidate)
    target_worktree: Path | None = None
    for worktree in sorted(family.worktrees, key=lambda item: len(item.parts), reverse=True):
        try:
            target.relative_to(worktree)
        except ValueError:
            continue
        target_worktree = worktree
        break
    if target_worktree is None:
        return False

    target_common = _resolve_git_path(
        target_worktree,
        _git_output(target_worktree, "rev-parse", "--git-common-dir"),
    )
    return target_common is not None and target_common == family.common_dir
