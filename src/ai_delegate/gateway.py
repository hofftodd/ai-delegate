"""Model gateway: a delegated agent reaches its model only through here.

Each job gets a secret base URL, ``http://<host>:<port>/g/<token>/v1``, written into pi's
provider config. The gateway forwards to ai-swap and, on the way (as ai-factory's does):

- pins the job's model, so the agent can't pick another one;
- counts model calls and *new* tokens against the job's budgets: the part of each prompt
  the model hadn't already seen in this job, plus the completion (ai-factory#18);
- spots the agent returning the same response over and over;
- refuses calls once the job is stopped or over budget, and tells the job why.

One gateway runs per ai-delegate process, on a random port, started with the first job.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

log = logging.getLogger("ai_delegate.gateway")

HOP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}


@dataclass
class Budget:
    max_requests: int = 80
    max_tokens: int = 400_000
    repeat_limit: int = 3


@dataclass
class Meter:
    """One job's use of its model, and the reason it was stopped, if it was."""

    model: str
    budget: Budget
    on_trip: Callable[[str], None]
    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    new_tokens: int = 0
    last_prompt: int = 0
    signatures: list[str] = field(default_factory=list)
    stopped: str = ""

    def trip(self, reason: str) -> None:
        if not self.stopped:
            self.stopped = reason
            self.on_trip(reason)

    def record(self, prompt: int, completion: int, cached: int, sig: str) -> None:
        self.tokens_in += prompt
        self.tokens_out += completion
        self.new_tokens += new_prompt_tokens(prompt, self.last_prompt, cached) + completion
        self.last_prompt = prompt
        if self.new_tokens > self.budget.max_tokens:
            self.trip(
                f"token budget exceeded ({self.new_tokens:,} new tokens > "
                f"{self.budget.max_tokens:,})"
            )
            return
        self.signatures.append(sig)
        n = self.budget.repeat_limit
        tail = self.signatures[-n:]
        if len(tail) == n and tail[0] and len(set(tail)) == 1:
            self.trip(f"the model returned the same response {n} times in a row")


def new_prompt_tokens(prompt: int, previous: int, cached: int = 0) -> int:
    """How much of a prompt is new: the growth since the last one (an agent's conversation
    only grows), or all of it after a compaction. A server's cached count is believed when
    it says less is new."""
    new = prompt - previous if prompt >= previous else prompt
    if cached > 0:
        new = min(new, max(0, prompt - cached))
    return max(0, new)


def signature(content: str, calls: list[dict[str, Any]]) -> str:
    joined = "|".join(
        f"{c.get('function', {}).get('name', '')}:{c.get('function', {}).get('arguments', '')}"
        for c in calls
    )
    return hashlib.sha256(f"{content.strip()}\x00{joined}".encode()).hexdigest()[:16]


def estimate_tokens(text: str) -> int:
    return len(text) // 4


class Gateway:
    def __init__(self, upstream: str, transport: httpx.AsyncBaseTransport | None = None):
        self.upstream = upstream.rstrip("/")
        self.meters: dict[str, Meter] = {}
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=900.0, write=60.0, pool=30.0),
            transport=transport,
        )
        self.app = Starlette(
            routes=[
                Route("/g/{token}/{path:path}", self.proxy, methods=["GET", "POST"]),
                Route("/health", lambda r: JSONResponse({"ok": True})),
            ]
        )
        self.port = 0
        self._server: Any = None
        self._task: asyncio.Task[None] | None = None

    def register(self, meter: Meter) -> str:
        token = secrets.token_urlsafe(24)
        self.meters[token] = meter
        return token

    def unregister(self, token: str) -> None:
        self.meters.pop(token, None)

    async def start(self, host: str) -> int:
        if self._task:
            return self.port
        import uvicorn

        config = uvicorn.Config(
            self.app, host=host, port=0, log_level="warning", access_log=False, lifespan="off"
        )
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.02)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        return self.port

    async def aclose(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._task:
            with contextlib.suppress(Exception):
                await self._task
        await self.client.aclose()

    # -- proxy ----------------------------------------------------------------------------

    async def proxy(self, request: Request) -> Response:
        meter = self.meters.get(request.path_params["token"])
        if meter is None:
            return _error(403, "unknown or finished job")
        if meter.stopped:
            return _error(403, f"job stopped: {meter.stopped}")
        path = request.path_params["path"].lstrip("/")
        url = f"{self.upstream}/{path}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        try:
            if request.method == "GET":
                r = await self.client.get(url, headers=headers, params=request.query_params)
                return _passthrough(r)
            body = await request.body()
            if not path.rstrip("/").endswith("chat/completions"):
                return _passthrough(await self.client.post(url, headers=headers, content=body))
            return await self._chat(meter, url, headers, body)
        except httpx.HTTPError as e:
            return _error(502, f"cannot reach ai-swap: {type(e).__name__}: {e}")

    async def _chat(self, meter: Meter, url: str, headers: dict[str, str], body: bytes) -> Response:
        meter.requests += 1
        if meter.requests > meter.budget.max_requests:
            meter.trip(f"model call budget exceeded ({meter.budget.max_requests} calls)")
            return _error(429, meter.stopped)
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return _error(400, "invalid JSON")
        payload["model"] = meter.model
        estimate = estimate_tokens(json.dumps(payload.get("messages", [])))
        if not payload.get("stream"):
            r = await self.client.post(url, headers=headers, json=payload)
            if r.status_code < 400:
                data = r.json()
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                usage = data.get("usage") or {}
                content = msg.get("content") or ""
                calls = msg.get("tool_calls") or []
                meter.record(
                    usage.get("prompt_tokens") or estimate,
                    usage.get("completion_tokens") or estimate_tokens(content),
                    _cached(usage),
                    signature(content, calls),
                )
            return _passthrough(r)

        payload.setdefault("stream_options", {})["include_usage"] = True
        req = self.client.build_request("POST", url, headers=headers, json=payload)
        resp = await self.client.send(req, stream=True)
        if resp.status_code >= 400:
            content = await resp.aread()
            await resp.aclose()
            return Response(
                content, status_code=resp.status_code, media_type=resp.headers.get("content-type")
            )

        async def relay() -> AsyncIterator[bytes]:
            text: list[str] = []
            calls: dict[int, dict[str, Any]] = {}
            usage: dict[str, Any] = {}
            buf = b""
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        parse_sse_line(line, text, calls, usage)
            finally:
                await resp.aclose()
                joined = "".join(text)
                tool_calls = [calls[i] for i in sorted(calls)]
                meter.record(
                    usage.get("prompt_tokens") or estimate,
                    usage.get("completion_tokens")
                    or estimate_tokens(joined + json.dumps(tool_calls)),
                    _cached(usage),
                    signature(joined, tool_calls),
                )

        return StreamingResponse(
            relay(),
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "text/event-stream"),
        )


def parse_sse_line(
    line: bytes, content: list[str], calls: dict[int, dict[str, Any]], usage: dict[str, Any]
) -> None:
    text = line.decode(errors="replace").strip()
    if not text.startswith("data:"):
        return
    data = text[5:].strip()
    if not data or data == "[DONE]":
        return
    try:
        chunk = json.loads(data)
    except ValueError:
        return
    if chunk.get("usage"):
        usage.update({k: v for k, v in chunk["usage"].items() if isinstance(v, int | dict) and v})
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or {}
        if delta.get("content"):
            content.append(delta["content"])
        for tc in delta.get("tool_calls") or []:
            slot = calls.setdefault(tc.get("index", 0), {"function": {"name": "", "arguments": ""}})
            fn = tc.get("function") or {}
            slot["function"]["name"] += fn.get("name") or ""
            slot["function"]["arguments"] += fn.get("arguments") or ""


def _cached(usage: dict[str, Any]) -> int:
    details = usage.get("prompt_tokens_details") or {}
    return int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0


def _passthrough(r: httpx.Response) -> Response:
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": f"ai-delegate gateway: {message}", "type": "ai_delegate_gateway"}},
        status_code=status,
    )
