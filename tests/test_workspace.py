import subprocess
from pathlib import Path

import pytest

from ai_delegate import workspace

ID = ["-c", "user.name=t", "-c", "user.email=t@t"]


def sh(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "src"
    r.mkdir()
    sh(r, "init", "-q")
    (r / "a.py").write_text("a = 1\n")
    (r / ".gitignore").write_text("secret.txt\n")
    sh(r, "add", ".")
    sh(r, *ID, "commit", "-qm", "init")
    (r / "a.py").write_text("a = 2\n")  # uncommitted edit
    (r / "new.py").write_text("n = 1\n")  # untracked
    (r / "secret.txt").write_text("ignored\n")  # ignored: not carried
    return r


async def test_prepare_carries_uncommitted(repo: Path, tmp_path: Path):
    dest = tmp_path / "job" / "repo"
    p = await workspace.prepare(repo, dest, include_uncommitted=True)
    assert (dest / "a.py").read_text() == "a = 2\n" and (dest / "new.py").exists()
    assert not (dest / "secret.txt").exists()
    assert p.carried == "1 changed file(s) and 1 untracked file(s)"
    assert sh(dest, "remote") == ""  # no way back to the caller's repo


async def test_prepare_bare_head(repo: Path, tmp_path: Path):
    dest = tmp_path / "job" / "repo"
    p = await workspace.prepare(repo, dest, include_uncommitted=False)
    assert (dest / "a.py").read_text() == "a = 1\n" and p.carried == "none"


async def test_collect_includes_commits_and_skips_artifacts(repo: Path, tmp_path: Path):
    dest = tmp_path / "job" / "repo"
    p = await workspace.prepare(repo, dest, include_uncommitted=True)
    (dest / "b.py").write_text("b = 1\n")
    sh(dest, "add", "b.py")
    sh(dest, *ID, "commit", "-qm", "agent committed anyway")
    (dest / "a.py").write_text("a = 3\n")
    (dest / "__pycache__").mkdir()
    (dest / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\0junk")
    ch = await workspace.collect(dest, p.base)
    assert ch.files == 2 and "b.py" in ch.patch and "pyc" not in ch.patch
    patch_file = tmp_path / "result.patch"
    patch_file.write_text(ch.patch)
    assert await workspace.apply_check(repo, patch_file) == ""
    sh(repo, "apply", str(patch_file))
    assert (repo / "a.py").read_text() == "a = 3\n" and (repo / "b.py").exists()
