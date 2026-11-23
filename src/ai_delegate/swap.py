"""The ai-swap client: the cluster's state, and streamed chat completions.

Everything goes through one ai-swap endpoint, which forwards to the machine that serves the
model. ``/api/status`` gives each machine's models, memory and reservation; ``/v1/models``
gives the id to send for each model (``name`` on the endpoint's own machine, ``name@node`` for
a copy on a peer, when the name alone would pick another machine's).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

import httpx
from ai_common.swap import auth_headers
from ai_common.swap import lease as swap_lease

ACTIVE = ("ready", "starting")  # loaded, or loading already: using it costs no swap


class SwapError(Exception):
    """ai-swap refused or couldn't be reached; the message is meant for the caller."""


@dataclass
class Machine:
    name: str
    online: bool = True
    budget_gb: float = 0.0
    used_gb: float = 0.0
    role: str = ""
    lock: dict[str, Any] | None = None

    @property
    def free_gb(self) -> float:
        return max(0.0, self.budget_gb - self.used_gb)

    @property
    def reserved_by(self) -> str | None:
        if not self.lock:
            return None
        who = self.lock.get("owner") or "someone"
        why = self.lock.get("reason")
        return f"{who} ({why})" if why else who


@dataclass
class Model:
    id: str  # what to send as "model"
    name: str
    machine: str
    state: str
    ctx: int | None = None
    memory_gb: float = 0.0
    resident: bool = False
    inflight: int = 0
    aliases: list[str] = field(default_factory=list)
    runnable: bool = True
    tps: float | None = None  # ai-swap's measured average generation speed
    engine: str = ""

    @property
    def pinned(self) -> str:
        """The name to send: pinned to the machine routing chose. ai-swap's placement may
        serve a bare name from another machine's copy."""
        return f"{self.name}@{self.machine}"

    @property
    def loaded(self) -> bool:
        return self.state in ACTIVE

    def matches(self, wanted: str) -> bool:
        w = wanted.lower()
        if w == self.id.lower():
            return True
        base, _, node = w.partition("@")
        if node and node != self.machine.lower():
            return False
        return base == self.name.lower() or base in (a.lower() for a in self.aliases)


@dataclass
class Cluster:
    machines: dict[str, Machine]
    models: list[Model]

    def find(self, wanted: str) -> list[Model]:
        return [m for m in self.models if m.matches(wanted)]


def parse_cluster(status: dict[str, Any], models: dict[str, Any]) -> Cluster:
    ids: dict[tuple[str, str], str] = {}
    for e in models.get("data") or []:
        mid, node = e.get("id"), e.get("node")
        if mid and node:
            ids[(node, mid.split("@", 1)[0])] = mid

    machines: dict[str, Machine] = {}
    out: list[Model] = []

    def add(node: dict[str, Any] | None, online: bool, name: str | None = None) -> None:
        name = (node or {}).get("name") or name
        if not name:
            return
        machines[name] = Machine(
            name=name,
            online=online,
            budget_gb=float((node or {}).get("memory_budget_gb") or 0),
            used_gb=float((node or {}).get("memory_used_gb") or 0),
            role=(node or {}).get("role") or "",
            lock=(node or {}).get("lock"),
        )
        if not online or not node:
            return
        for x in node.get("models") or []:
            mname = x.get("name")
            if not mname or (name, mname) not in ids:
                continue  # ai-swap doesn't offer it (offline, or not runnable here)
            out.append(
                Model(
                    id=ids[(name, mname)],
                    name=mname,
                    machine=name,
                    state=x.get("state") or "unknown",
                    ctx=x.get("ctx") or None,
                    memory_gb=float(x.get("memory_gb") or 0),
                    resident=bool(x.get("resident")),
                    inflight=int(x.get("inflight") or 0),
                    aliases=list(x.get("aliases") or []),
                    runnable=bool(x.get("runnable", True)),
                    engine=x.get("engine") or "",
                )
            )

    add(status.get("node"), True)
    for p in status.get("peers") or []:
        add(p.get("node"), bool(p.get("online")), p.get("name"))
    return Cluster(machines, out)


@dataclass
class Reply:
    text: str
    reasoning_chars: int = 0
    finish: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    seconds: float = 0.0
    first_token_s: float | None = None
    timed_out: bool = False
    # Stopped because the answer kept repeating a short run of text (see _loop_unit).
    looped: bool = False

    @property
    def tps(self) -> float | None:
        if not self.completion_tokens or self.first_token_s is None:
            return None
        gen = self.seconds - self.first_token_s
        return self.completion_tokens / gen if gen > 0 else None


Progress = Callable[[int, str], Awaitable[None]]


class AiSwap:
    """ai-swap's API. Given several endpoints (the same ai-swap by different routes, such as
    its LAN name and its Tailscale name), it uses the first that answers and moves to the
    next when a connection fails: away from home, ``studio.local`` doesn't resolve but the
    Tailscale name does."""

    def __init__(
        self,
        endpoint: str | list[str],
        transport: httpx.AsyncBaseTransport | None = None,
        api_key: str = "",
    ):
        self.endpoints = [
            e.rstrip("/") for e in ([endpoint] if isinstance(endpoint, str) else endpoint)
        ]
        self.endpoint = self.endpoints[0]
        self._transport = transport
        self.api_key = api_key
        self._headers = auth_headers(api_key)

    async def _failover(self) -> bool:
        """Point at the first endpoint that answers /api/status, current one last; True if
        one did."""
        order = [e for e in self.endpoints if e != self.endpoint] + [self.endpoint]
        for e in order:
            try:
                async with httpx.AsyncClient(
                    timeout=4.0, transport=self._transport, headers=self._headers
                ) as c:
                    if (await c.get(f"{e}/api/status")).status_code == 200:
                        self.endpoint = e
                        return True
            except httpx.HTTPError:
                continue
        return False

    def _client(self, timeout: httpx.Timeout | float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.endpoint, timeout=timeout, transport=self._transport,
            headers=self._headers,
        )  # fmt: skip

    def lease(self, model: str, owner: str, ttl_s: int = 600) -> AbstractAsyncContextManager[Any]:
        """Hold an ai-swap lease on ``model`` (an id like ``flint@studio``) while the block
        runs, so another client's load can't evict it between requests. A no-op against an
        ai-swap without leases."""
        return swap_lease(
            self.endpoint, model, owner, ttl_s=ttl_s, api_key=self.api_key,
            transport=self._transport,
        )  # fmt: skip

    async def cluster(self, with_speed: bool = False) -> Cluster:
        try:
            return await self._cluster(with_speed)
        except SwapError as e:
            if len(self.endpoints) < 2 or not isinstance(e.__cause__, httpx.TransportError):
                raise
            if not await self._failover():
                raise SwapError(
                    f"can't reach ai-swap at any of {', '.join(self.endpoints)}: {e.__cause__}"
                ) from e.__cause__
            return await self._cluster(with_speed)

    async def _cluster(self, with_speed: bool) -> Cluster:
        try:
            async with self._client(5.0) as c:
                st, md = await asyncio.gather(c.get("/api/status"), c.get("/v1/models"))
                st.raise_for_status()
                md.raise_for_status()
                cluster = parse_cluster(st.json(), md.json())
                if with_speed:
                    await asyncio.gather(*(self._speed(c, m) for m in cluster.models))
                return cluster
        except httpx.HTTPError as e:
            raise SwapError(f"can't read ai-swap's status at {self.endpoint}: {e}") from e
        except (ValueError, KeyError, TypeError) as e:
            raise SwapError(f"{self.endpoint} doesn't look like ai-swap: {e}") from e

    async def _speed(self, c: httpx.AsyncClient, m: Model) -> None:
        try:
            r = await c.get(f"/api/models/{m.id}/stats", timeout=3.0)
            if r.status_code == 200:
                tps = ((r.json() or {}).get("requests") or {}).get("avg_gen_tps")
                m.tps = float(tps) if tps else None
        except (httpx.HTTPError, ValueError, TypeError):
            pass

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float | None,
        deadline_s: float,
        progress: Progress | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Reply:
        """Stream one completion. Past ``deadline_s`` it returns what it has, marked timed out."""
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if temperature is not None:
            body["temperature"] = temperature
        body.update(extra or {})
        reply = Reply(text="")
        parts: list[str] = []
        start = time.monotonic()
        last_note = start
        tail, unseen = "", 0
        unit: str | None = None
        # A model being loaded can take minutes before the first byte, so only the overall
        # deadline bounds the wait; connect stays short so a dead endpoint fails fast.
        timeout = httpx.Timeout(deadline_s, connect=10.0)
        try:
            async with (
                asyncio.timeout(deadline_s),
                self._client(timeout) as c,
                c.stream("POST", "/v1/chat/completions", json=body) as r,
            ):
                if r.status_code != 200:
                    await r.aread()
                    raise SwapError(_error_text(r))
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        ev = json.loads(data)
                    except ValueError:
                        continue
                    if usage := ev.get("usage"):
                        reply.prompt_tokens = usage.get("prompt_tokens")
                        reply.completion_tokens = usage.get("completion_tokens")
                    for ch in ev.get("choices") or []:
                        delta = ch.get("delta") or {}
                        thought = delta.get("reasoning_content") or delta.get("reasoning")
                        if (thought or delta.get("content")) and reply.first_token_s is None:
                            reply.first_token_s = time.monotonic() - start
                        if thought:
                            reply.reasoning_chars += len(thought)
                        if text := delta.get("content"):
                            parts.append(text)
                            tail = (tail + text)[-LOOP_TAIL:]
                            unseen += len(text)
                        if ch.get("finish_reason"):
                            reply.finish = ch["finish_reason"]
                    if unseen >= 64:
                        unseen = 0
                        if unit := _loop_unit(tail):
                            reply.looped, reply.finish = True, "loop"
                            break
                    now = time.monotonic()
                    if progress and now - last_note >= 2.0:
                        last_note = now
                        phase = "writing" if parts else "thinking"
                        await progress(int(now - start), f"{model}: {phase}")
        except TimeoutError:
            reply.timed_out = True
        except httpx.HTTPError as e:
            raise SwapError(f"request to {model} through {self.endpoint} failed: {e}") from e
        reply.text = "".join(parts)
        if unit:
            while reply.text.endswith(unit * 2):
                reply.text = reply.text[: -len(unit)]
            reply.text += " […]"
        reply.seconds = time.monotonic() - start
        return reply


# A reply whose last LOOP_RUN characters (at least LOOP_REPEATS copies) are one unit of up
# to LOOP_UNIT characters repeated is stuck: near-greedy decoding can loop until the output
# budget runs out ("0000…"), so chat stops there and trims the run to one copy.
LOOP_UNIT = 100
LOOP_RUN = 400
LOOP_REPEATS = 5
LOOP_TAIL = LOOP_UNIT * LOOP_REPEATS + LOOP_RUN


def _loop_unit(tail: str) -> str | None:
    """The unit the end of `tail` keeps repeating, or None."""
    for size in range(1, LOOP_UNIT + 1):
        run = max(LOOP_RUN, size * LOOP_REPEATS)
        if len(tail) < run:
            break
        unit = tail[-size:]
        if unit.strip() and tail[-run:] == (unit * (run // size + 1))[-run:]:
            return unit
    return None


def _error_text(r: httpx.Response) -> str:
    msg = r.text.strip()
    try:
        err = r.json().get("error")
        if isinstance(err, dict):
            msg = err.get("message") or msg
        elif isinstance(err, str):
            msg = err
    except ValueError:
        pass
    retry = r.headers.get("Retry-After")
    tail = f" (retry after {retry} s)" if retry else ""
    return f"ai-swap answered {r.status_code}: {msg}{tail}"
