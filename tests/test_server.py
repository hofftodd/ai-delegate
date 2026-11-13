import subprocess

import pytest

from ai_delegate import server
from ai_delegate.routing import pick
from ai_delegate.swap import Reply


def test_render_thinking_cut_off(cluster):
    choice = pick(cluster, ["thinker"])
    out = server.render(Reply(text="", reasoning_chars=900, finish="length"), choice, 8192, 60)
    assert "whole 8,192-token output budget reasoning" in out
    assert out.rstrip().endswith("0.0 s")
    assert "thinker on mini · loaded" in out


def test_render_timeout_and_skipped(cluster):
    choice = pick(cluster, ["thinker"])
    choice.skipped = ["x: gone"]
    out = server.render(Reply(text="half", timed_out=True, seconds=60), choice, 100, 60)
    assert out.startswith("> Stopped at the 60 s time limit")
    assert "skipped: x: gone" in out


def test_attach_limit(monkeypatch):
    monkeypatch.setattr(server.cfg, "max_input_bytes", 50)
    with pytest.raises(server.InputError, match="over the 50-byte limit"):
        server.attach("q", [("a.py", "x" * 100)], None)


def test_read_files_rejects_missing_and_binary(tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\0\1\2")
    (tmp_path / "t.txt").write_text("hi")
    assert server.read_files(["t.txt"], tmp_path) == [("t.txt", "hi")]
    with pytest.raises(server.InputError, match="looks binary"):
        server.read_files(["b.bin"], tmp_path)
    with pytest.raises(server.InputError, match="no such file"):
        server.read_files(["nope"], tmp_path)


async def test_git_diff(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "f.py").write_text("a = 1\n")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    (tmp_path / "f.py").write_text("a = 2\n")
    assert "+a = 2" in await server.git_diff(tmp_path, "HEAD")
    with pytest.raises(server.InputError, match="git diff nope failed"):
        await server.git_diff(tmp_path, "nope")
