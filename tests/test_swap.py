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
