"""A job's copy of the repo, and the diff it hands back.

A job works in a local ``git clone`` of the caller's repo rather than a ``git worktree``: a
worktree's ``.git`` is a file pointing into the caller's ``.git``, a path that doesn't exist
inside the sandbox container. A local clone hard-links the object store (cheap), is
self-contained, and never adds branches to the caller's repo.

The clone starts at the caller's HEAD plus, by default, their uncommitted changes (tracked
edits and untracked files that aren't ignored), committed in the clone as the job's *base*.
The result is ``git diff <base>`` after the agent finishes, so it applies to the caller's
working tree as it was when the job started.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

# Never part of a job's diff even when the repo doesn't ignore them: what running tests and
# tools leaves behind (the agent runs them, and so does the test command).
ARTIFACTS = [
    "__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".venv/",
    "node_modules/", ".coverage", "coverage/", ".tox/", "*.egg-info/", ".DS_Store",
]  # fmt: skip
IDENTITY = ["-c", "user.name=ai-delegate", "-c", "user.email=ai-delegate@localhost"]


class WorkspaceError(Exception):
    pass


async def git(cwd: Path, *args: str, input: bytes | None = None, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(cwd),
        *args,
        stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(input)
    if check and proc.returncode != 0:
        msg = err.decode(errors="replace").strip()
        raise WorkspaceError(f"git {' '.join(args[:3])} failed: {msg}")
    return out.decode(errors="replace")


async def repo_root(path: Path) -> Path:
    try:
        return Path((await git(path, "rev-parse", "--show-toplevel")).strip())
    except WorkspaceError as e:
        raise WorkspaceError(f"{path} is not inside a git repository") from e


@dataclass
class Prepared:
    source: Path
    head: str
    base: str
    carried: str  # what uncommitted work came along, for the result


async def prepare(source: Path, dest: Path, include_uncommitted: bool) -> Prepared:
    head = (await git(source, "rev-parse", "HEAD")).strip()
    dest.parent.mkdir(parents=True, exist_ok=True)
    await git(source.parent, "clone", "--quiet", "--local", "--no-checkout", str(source), str(dest))
    await git(dest, "remote", "remove", "origin")
    exclude = dest / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as f:
        f.write("\n# ai-delegate: tool and test artifacts\n" + "\n".join(ARTIFACTS) + "\n")
    await git(dest, "checkout", "--quiet", "--detach", head)

    carried = "none"
    if include_uncommitted:
        patch = await git(source, "diff", "HEAD", "--binary", "--no-color", "--no-ext-diff")
        untracked = [
            p
            for p in (await git(source, "ls-files", "--others", "--exclude-standard", "-z")).split(
                "\0"
            )
            if p
        ]
        if patch.strip():
            await git(dest, "apply", "--binary", "--whitespace=nowarn", input=patch.encode())
        for rel in untracked:
            src = source / rel
            if src.is_file():
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest / rel)
        n_tracked = patch.count("\ndiff --git ") + patch.startswith("diff --git ")
        parts = [f"{n_tracked} changed file(s)"] if n_tracked else []
        parts += [f"{len(untracked)} untracked file(s)"] if untracked else []
        if parts:
            carried = " and ".join(parts)
    await git(dest, "add", "-A")
    await git(dest, *IDENTITY, "commit", "--quiet", "--allow-empty", "-m", "ai-delegate base")
    base = (await git(dest, "rev-parse", "HEAD")).strip()
    return Prepared(source, head, base, carried)


@dataclass
class Change:
    patch: str
    stat: str
    added: int
    removed: int
    files: int

    @property
    def lines(self) -> int:
        return self.added + self.removed


async def collect(dest: Path, base: str) -> Change:
    """Everything the agent changed, commits included, as one diff against the base."""
    await git(dest, "add", "-A")
    patch = await git(dest, "diff", "--cached", "--binary", "--no-color", "--no-ext-diff", base)
    stat = await git(dest, "diff", "--cached", "--stat", "--no-color", base)
    added = removed = files = 0
    for row in (await git(dest, "diff", "--cached", "--numstat", base)).splitlines():
        a, r, *_ = row.split("\t")
        files += 1
        added += int(a) if a.isdigit() else 0
        removed += int(r) if r.isdigit() else 0
    return Change(patch, stat.strip(), added, removed, files)


async def changed_lines(dest: Path, base: str) -> int:
    """Lines changed so far, cheaply, for the running budget check (untracked not counted)."""
    out = await git(dest, "diff", "--numstat", base, check=False)
    total = 0
    for row in out.splitlines():
        a, r, *_ = (row.split("\t") + ["", ""])[:3]
        total += (int(a) if a.isdigit() else 0) + (int(r) if r.isdigit() else 0)
    return total


async def apply_check(source: Path, patch_file: Path) -> str:
    """'' if the patch applies cleanly to the caller's working tree now, else why not."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(source),
        "apply",
        "--check",
        str(patch_file),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    return "" if proc.returncode == 0 else err.decode(errors="replace").strip()
