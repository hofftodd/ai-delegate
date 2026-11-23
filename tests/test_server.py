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


def test_keys_from_env_file_and_environment(tmp_path, monkeypatch):
    from ai_delegate import config

    monkeypatch.delenv("AI_SWAP_API_KEY", raising=False)
    monkeypatch.delenv("AI_DELEGATE_SEARCH_TOKEN", raising=False)
    (tmp_path / "config.yaml").write_text("endpoint: http://swap\n")
    (tmp_path / "env").write_text(
        "# rendered\nAI_SWAP_API_KEY=from-file\nAI_DELEGATE_SEARCH_TOKEN='tok'\n"
    )
    cfg = config.load(tmp_path / "config.yaml")
    assert cfg.api_key == "from-file" and cfg.web.search_token == "tok"
    monkeypatch.setenv("AI_SWAP_API_KEY", "from-env")
    assert config.load(tmp_path / "config.yaml").api_key == "from-env"


def test_empty_api_key_in_config_is_no_key(tmp_path, monkeypatch):
    from ai_delegate import config

    monkeypatch.delenv("AI_SWAP_API_KEY", raising=False)
    (tmp_path / "config.yaml").write_text("endpoint: http://swap\napi_key:\n")
    assert config.load(tmp_path / "config.yaml").api_key == ""


def test_model_settings_from_config(tmp_path):
    from ai_delegate import config

    (tmp_path / "config.yaml").write_text(
        "models:\n"
        "  qwen3.8-flash-next:\n"
        "    context: 262144\n"
        "    thinking_budget: 0.75\n"
        "    params: {top_k: 20}\n"
    )
    cfg = config.load(tmp_path / "config.yaml")
    s = config.settings_for(cfg, lambda w: w == "qwen3.8-flash-next")
    assert (s.context, s.thinking_budget, s.params) == (262144, 0.75, {"top_k": 20})
    assert config.settings_for(cfg, lambda w: False).context is None
    assert server.request_extra(s, 32768) == {"top_k": 20, "thinking_budget": 24576}
    assert server.request_extra(config.ModelSettings(), 32768) == {}


@pytest.mark.parametrize(
    "section, message",
    [("    thinking_budget: 2\n", "between 0 and 1"), ("    budget: 1\n", "unknown setting")],
)
def test_model_settings_rejects_bad_values(tmp_path, section, message):
    from ai_delegate import config

    (tmp_path / "config.yaml").write_text("models:\n  m:\n" + section)
    with pytest.raises(ValueError, match=message):
        config.load(tmp_path / "config.yaml")


def test_a_tool_can_set_its_own_thinking_share(tmp_path):
    from ai_delegate import config

    (tmp_path / "config.yaml").write_text(
        "review:\n  thinking_budget: 0.375\nmodels:\n  m:\n    thinking_budget: 0.75\n"
    )
    cfg = config.load(tmp_path / "config.yaml")
    assert cfg.tool_thinking == {"review": 0.375}
    (tmp_path / "config.yaml").write_text("review:\n  thinking_budget: 1.5\n")
    with pytest.raises(ValueError, match="review.thinking_budget"):
        config.load(tmp_path / "config.yaml")


async def test_run_sends_the_tools_thinking_share(cluster, monkeypatch):
    from ai_delegate import config

    seen = {}

    class FakeSwap:
        async def cluster(self):
            return cluster

        async def chat(self, model, messages, **kw):
            seen.update(kw)
            return Reply(text="No findings.", finish="stop")

    class Ctx:
        async def report_progress(self, *a, **k):
            pass

    monkeypatch.setattr(server, "swap", FakeSwap())
    monkeypatch.setattr(server.outcomes, "log_call", lambda *a, **k: None)
    monkeypatch.setattr(
        server.cfg, "models", {"flint": config.ModelSettings(context=131072, thinking_budget=0.75)}
    )
    monkeypatch.setattr(server.cfg, "tool_thinking", {"review": 0.375})
    await server.run("review", "sys", "diff", "flint", 0.2, 5, Ctx())
    assert seen["max_tokens"] == 32768 and seen["extra"] == {"thinking_budget": 12288}
    await server.run("ask", "sys", "q", "flint", 0.2, 5, Ctx())
    assert seen["extra"] == {"thinking_budget": 24576}
