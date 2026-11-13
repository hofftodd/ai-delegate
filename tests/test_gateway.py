import json

import httpx

from ai_delegate.gateway import Budget, Gateway, Meter, new_prompt_tokens


def sse_body(content: str, prompt: int, completion: int) -> str:
    ev = [
        {"choices": [{"delta": {"content": content}}]},
        {"choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": completion}},
    ]
    return "".join(f"data: {json.dumps(e)}\n\n" for e in ev) + "data: [DONE]\n\n"


def upstream(seen: list[dict], content: str = "ok", prompt: int = 100, completion: int = 10):
    def h(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        if body.get("stream"):
            return httpx.Response(
                200,
                text=sse_body(content, prompt, completion),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
            },
        )

    return httpx.MockTransport(h)


async def call(gw: Gateway, token: str, stream: bool) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gw.app), base_url="http://gw"
    ) as c:
        r = await c.post(
            f"/g/{token}/v1/chat/completions",
            json={
                "model": "other",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": stream,
            },
        )
        await r.aread()
        return r


def test_new_prompt_tokens():
    assert new_prompt_tokens(1000, 0) == 1000
    assert new_prompt_tokens(1500, 1000) == 500
    assert new_prompt_tokens(400, 1500) == 400  # compacted: all new
    assert new_prompt_tokens(1500, 0, cached=1400) == 100


async def test_pins_model_and_counts_stream():
    seen: list[dict] = []
    trips: list[str] = []
    gw = Gateway("http://swap", transport=upstream(seen, prompt=100, completion=10))
    m = Meter("flint", Budget(), trips.append)
    tok = gw.register(m)
    r = await call(gw, tok, stream=True)
    assert r.status_code == 200 and "ok" in r.text
    assert seen[0]["model"] == "flint" and seen[0]["stream_options"]["include_usage"]
    assert (m.requests, m.tokens_in, m.tokens_out, m.new_tokens) == (1, 100, 10, 110)
    assert not trips


async def test_request_budget_and_refusal_after_trip():
    seen: list[dict] = []
    trips: list[str] = []
    gw = Gateway("http://swap", transport=upstream(seen))
    m = Meter("flint", Budget(max_requests=1, repeat_limit=99), trips.append)
    tok = gw.register(m)
    assert (await call(gw, tok, stream=False)).status_code == 200
    r = await call(gw, tok, stream=False)
    assert r.status_code == 429 and "model call budget exceeded" in r.text
    assert trips == ["model call budget exceeded (1 calls)"]
    assert (await call(gw, tok, stream=False)).status_code == 403
    assert len(seen) == 1


async def test_repeat_and_token_trips():
    trips: list[str] = []
    gw = Gateway("http://swap", transport=upstream([], content="same"))
    m = Meter("flint", Budget(repeat_limit=3), trips.append)
    tok = gw.register(m)
    for _ in range(3):
        await call(gw, tok, stream=True)
    assert trips == ["the model returned the same response 3 times in a row"]

    trips.clear()
    gw = Gateway("http://swap", transport=upstream([], prompt=900, completion=200))
    m = Meter("flint", Budget(max_tokens=1000), trips.append)
    await call(gw, gw.register(m), stream=False)
    assert trips and trips[0].startswith("token budget exceeded (1,100 new tokens")


async def test_unknown_token():
    gw = Gateway("http://swap", transport=upstream([]))
    assert (await call(gw, "nope", stream=False)).status_code == 403
