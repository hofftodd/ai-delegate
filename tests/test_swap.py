import asyncio
import json

import httpx
import pytest
from conftest import models, status

from ai_delegate.swap import AiSwap, SwapError


def sse(*events, done=True):
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    return body + ("data: [DONE]\n\n" if done else "")


def chunk(content=None, reasoning=None, finish=None):
    delta = {}
    if content:
        delta["content"] = content
    if reasoning:
        delta["reasoning_content"] = reasoning
    return {"choices": [{"delta": delta, "finish_reason": finish}]}


def client(handler):
    return AiSwap("http://swap", transport=httpx.MockTransport(handler))


async def test_cluster_reads_both_endpoints():
    def h(req):
        if req.url.path == "/api/status":
            return httpx.Response(200, json=status())
        return httpx.Response(200, json=models())

    cl = await client(h).cluster()
    assert len(cl.models) == 5


async def test_cluster_unreachable():
    def h(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(SwapError, match="can't read ai-swap's status"):
        await client(h).cluster()


async def test_chat_streams_and_skips_reasoning():
    seen = {}

    def h(req):
        seen.update(json.loads(req.content))
        body = sse(
            chunk(reasoning="hmm"),
            chunk(content="Hel"),
            chunk(content="lo", finish="stop"),
            {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}},
        )
        return httpx.Response(200, text=body)

    r = await client(h).chat("flint", [], max_tokens=100, temperature=0.2, deadline_s=5)
    assert (r.text, r.finish, r.reasoning_chars) == ("Hello", "stop", 3)
    assert (r.prompt_tokens, r.completion_tokens) == (5, 3)
    assert seen["model"] == "flint" and seen["stream"] and seen["temperature"] == 0.2


async def test_chat_reservation_error_message():
    def h(req):
        err = {"error": {"message": "mini is reserved by ai-bench"}}
        return httpx.Response(503, json=err, headers={"Retry-After": "30"})

    with pytest.raises(SwapError, match=r"503: mini is reserved by ai-bench \(retry after 30 s\)"):
        await client(h).chat("thinker", [], max_tokens=10, temperature=None, deadline_s=5)


async def test_chat_deadline_returns_partial():
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield f"data: {json.dumps(chunk(content='partial'))}\n\n".encode()
            await asyncio.sleep(10)

    def h(req):
        return httpx.Response(200, stream=Slow())

    r = await client(h).chat("flint", [], max_tokens=10, temperature=None, deadline_s=0.3)
    assert r.timed_out and r.text == "partial"


# -- several endpoints: LAN name first, Tailscale name next ------------------------------


def two_routes(down: set[str]):
    """ai-swap reachable as http://home and http://ts; hosts in ``down`` refuse connections
    (as studio.local does away from home)."""
    seen: list[str] = []

    def h(req):
        seen.append(req.url.host)
        if req.url.host in down:
            raise httpx.ConnectError("nodename nor servname provided")
        if req.url.path == "/api/status":
            return httpx.Response(200, json=status())
        return httpx.Response(200, json=models())

    return AiSwap(["http://home/", "http://ts"], transport=httpx.MockTransport(h)), seen


async def test_first_endpoint_used_when_it_answers():
    swap, seen = two_routes(down=set())
    await swap.cluster()
    assert swap.endpoint == "http://home" and set(seen) == {"home"}


async def test_fails_over_and_stays():
    swap, seen = two_routes(down={"home"})
    cl = await swap.cluster()
    assert len(cl.models) == 5 and swap.endpoint == "http://ts"
    seen.clear()
    await swap.cluster()  # sticks with the endpoint that works
    assert set(seen) == {"ts"}


async def test_fails_back_when_the_current_one_goes():
    swap, _ = two_routes(down={"ts"})  # on Tailscale, then it drops and home answers
    swap.endpoint = "http://ts"
    await swap.cluster()
    assert swap.endpoint == "http://home"


async def test_all_endpoints_down():
    swap, _ = two_routes(down={"home", "ts"})
    with pytest.raises(SwapError, match="can't reach ai-swap at any of http://home, http://ts"):
        await swap.cluster()


async def test_http_errors_dont_fail_over():
    """A 500 means ai-swap answered: switching routes wouldn't help."""

    def h(req):
        return httpx.Response(500, text="boom")

    swap = AiSwap(["http://home", "http://ts"], transport=httpx.MockTransport(h))
    with pytest.raises(SwapError, match="can't read ai-swap's status at http://home"):
        await swap.cluster()
    assert swap.endpoint == "http://home"


def test_config_endpoint_list_and_env(tmp_path, monkeypatch):
    from ai_delegate import config

    monkeypatch.delenv("AI_DELEGATE_ENDPOINT", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("endpoint:\n  - http://studio.local:8080/\n  - http://studio.ts.net:8080\n")
    cfg = config.load(path, tmp_path / "routes.yaml")
    assert cfg.endpoints == ["http://studio.local:8080", "http://studio.ts.net:8080"]
    assert cfg.endpoint == "http://studio.local:8080"
    path.write_text("endpoint: http://one:8080\n")
    assert config.load(path, tmp_path / "routes.yaml").endpoints == ["http://one:8080"]
    monkeypatch.setenv("AI_DELEGATE_ENDPOINT", "http://a, http://b/")
    assert config.load(path, tmp_path / "routes.yaml").endpoints == ["http://a", "http://b"]
