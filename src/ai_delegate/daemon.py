"""The job daemon: one per user per machine, so jobs outlive the session that started them.

Claude Code starts one ai-delegate process per session (``serve``, over stdio). Those keep
the quick tools (ask, review, models, capabilities) and the session's working directory;
the job tools go through :class:`Client` to this daemon, which owns the job manager, the
gateway and the Docker relay. See DESIGN.md, Persistent job daemon.

- **Socket:** ``<state>/daemon.sock`` (state dir 0700, socket 0600): only this user can
  reach it, and nothing listens on the network.
- **API**, JSON over HTTP on the socket: ``POST /jobs`` (start), ``POST /jobs/{id}/revise``,
  ``POST /jobs/{id}/cancel``, ``DELETE /jobs/{id}``, ``GET /health``, ``POST /shutdown``.
  Reads need no API: the front end loads ``job.json`` and the job's files itself, since
  both sides share the state dir.
- **One at a time:** the daemon holds an exclusive flock on ``<state>/daemon.lock`` for its
  whole life; a second one started in a race exits at once.
- **Started on demand** by the first job call, detached, logging to ``<state>/daemon.log``.
  It exits after ``jobs.daemon_idle_exit_s`` with no job running and no request (0: never).
- **Upgrades:** ``/health`` reports a hash of the code. A front end on other code replaces
  an idle daemon at once; a busy one is told to exit when idle and keeps serving until then.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import os
import socket
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from ai_delegate import jobs as jobs_mod
from ai_delegate.config import Config
from ai_delegate.jobs import ACTIVE, Job, JobError

CONNECT_WAIT_S = 15.0  # for a freshly started daemon to answer /health
IDLE_CHECK_S = 15.0


def state_dir() -> Path:
    return jobs_mod.STATE_DIR


def socket_path() -> Path:
    return state_dir() / "daemon.sock"


def lock_path() -> Path:
    return state_dir() / "daemon.lock"


def log_path() -> Path:
    return state_dir() / "daemon.log"


def code_version() -> str:
    """A hash of this package's source: changes with any edit, committed or not."""
    h = hashlib.sha256()
    for p in sorted(Path(__file__).parent.glob("*.py")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def job_from(doc: dict[str, Any]) -> Job:
    known = set(Job.__dataclass_fields__)
    return Job(**{k: v for k, v in doc.items() if k in known})


# -- the daemon -----------------------------------------------------------------------------


class Daemon:
    def __init__(self, cfg: Config, manager: jobs_mod.Manager):
        self.cfg = cfg
        self.manager = manager
        self.version = code_version()
        self.started = time.time()
        self.last_request = time.monotonic()
        self.exit_when_idle = False
        self._server: Any = None

    # -- state ------------------------------------------------------------------------------

    def running(self) -> list[Job]:
        return [j for j in self.manager.jobs.values() if j.state in ACTIVE]

    def busy(self) -> bool:
        return bool(self.manager.tasks) or bool(self.running())

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "version": self.version,
            "pid": os.getpid(),
            "started": self.started,
            "running": [j.id for j in self.running()],
            "idle_s": round(time.monotonic() - self.last_request),
            "idle_exit_s": self.cfg.jobs.daemon_idle_exit_s,
            "exit_when_idle": self.exit_when_idle,
        }

    def should_exit(self) -> bool:
        if self.busy():
            return False
        if self.exit_when_idle:
            return True
        limit = self.cfg.jobs.daemon_idle_exit_s
        return bool(limit) and time.monotonic() - self.last_request > limit

    # -- HTTP -------------------------------------------------------------------------------

    def app(self) -> Any:
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        def job_json(job: Job, status: int = 200) -> JSONResponse:
            return JSONResponse(asdict(job), status)

        async def guarded(request: Request, fn: Any) -> JSONResponse:
            self.last_request = time.monotonic()
            try:
                return await fn(request)
            except (JobError, ValueError, OSError) as e:
                return JSONResponse({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001 - reported to the caller, which shows it
                from ai_delegate.routing import NoModel
                from ai_delegate.swap import SwapError

                if isinstance(e, NoModel | SwapError):
                    return JSONResponse({"error": str(e)}, 400)
                return JSONResponse({"error": f"{type(e).__name__}: {e}"}, 500)

        async def start(request: Request) -> JSONResponse:
            # A daemon told to exit when idle still takes new jobs (they keep it busy):
            # refusing would leave the caller nowhere to run them.
            async def go(request: Request) -> JSONResponse:
                b = await request.json()
                job = await self.manager.start(
                    str(b["task"]),
                    str(b["done_when"]),
                    Path(b["repo"]),
                    model=b.get("model"),
                    test_command=b.get("test_command"),
                    include_uncommitted=bool(b.get("include_uncommitted", True)),
                    time_limit_s=b.get("time_limit_s"),
                    sandbox_mode=b.get("sandbox_mode"),
                    kind=str(b.get("kind", "change")),
                    depth=b.get("depth"),
                )
                return job_json(job, 201)

            return await guarded(request, go)

        async def revise(request: Request) -> JSONResponse:
            async def go(request: Request) -> JSONResponse:
                b = await request.json()
                job = await self.manager.revise(
                    request.path_params["id"], str(b["feedback"]), b.get("time_limit_s")
                )
                return job_json(job)

            return await guarded(request, go)

        async def cancel(request: Request) -> JSONResponse:
            async def go(request: Request) -> JSONResponse:
                return job_json(await self.manager.cancel(request.path_params["id"]))

            return await guarded(request, go)

        async def discard(request: Request) -> JSONResponse:
            async def go(request: Request) -> JSONResponse:
                await self.manager.discard(request.path_params["id"])
                return JSONResponse({"discarded": request.path_params["id"]})

            return await guarded(request, go)

        async def health(request: Request) -> JSONResponse:
            return JSONResponse(self.health())  # doesn't count as activity

        async def shutdown(request: Request) -> JSONResponse:
            b = await request.json() if await request.body() else {}
            if b.get("when", "idle") == "now":
                if self.busy():
                    return JSONResponse({"error": "jobs are running; use when=idle"}, 409)
                self._stop()
                return JSONResponse({"stopping": True})
            self.exit_when_idle = True
            return JSONResponse({"stopping": "when idle", "running": len(self.running())})

        return Starlette(
            routes=[
                Route("/jobs", start, methods=["POST"]),
                Route("/jobs/{id}/revise", revise, methods=["POST"]),
                Route("/jobs/{id}/cancel", cancel, methods=["POST"]),
                Route("/jobs/{id}", discard, methods=["DELETE"]),
                Route("/health", health, methods=["GET"]),
                Route("/shutdown", shutdown, methods=["POST"]),
            ]
        )

    def _stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True

    async def _idle_watch(self) -> None:
        while True:
            await asyncio.sleep(IDLE_CHECK_S)
            if self.should_exit():
                self._stop()
                return

    async def serve(self, sock: Path) -> None:
        import uvicorn

        # Bound here, not by uvicorn: uvicorn chmods a socket it creates to 0666. Created
        # under umask 177, it is 0600 from the start.
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            listener.bind(str(sock))
        finally:
            os.umask(old_umask)
        config = uvicorn.Config(self.app(), log_level="warning", access_log=False, lifespan="off")
        self._server = uvicorn.Server(config)
        watch = asyncio.create_task(self._idle_watch())
        serving = asyncio.create_task(self._server.serve(sockets=[listener]))
        while not self._server.started and not serving.done():
            await asyncio.sleep(0.02)
        if self._server.started:
            _log(f"ai-delegate daemon {self.version} (pid {os.getpid()}) on {sock}")
        try:
            await serving
        finally:
            watch.cancel()
            await self.manager.aclose()
            with contextlib.suppress(FileNotFoundError):
                sock.unlink()
            _log(f"daemon {os.getpid()} exited")


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def run(cfg: Config) -> int:
    """``ai-delegate daemon``: serve until idle. Exits 0 at once if another daemon holds the
    lock (a race between two sessions starting one)."""
    from ai_delegate.swap import AiSwap

    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    lock = lock_path().open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _log("another ai-delegate daemon is running; exiting")
        return 0
    lock.seek(0)
    lock.truncate()
    lock.write(f"{os.getpid()}\n")
    lock.flush()
    sock = socket_path()
    with contextlib.suppress(FileNotFoundError):
        sock.unlink()  # stale: the lock says no daemon owns it
    manager = jobs_mod.Manager(cfg, AiSwap(cfg.endpoints))
    asyncio.run(Daemon(cfg, manager).serve(sock))
    return 0


# -- the front end's side -------------------------------------------------------------------


class DaemonError(JobError):
    """The daemon couldn't be started or reached (a JobError, so every tool reports it)."""


class Client:
    """What the MCP tools use in place of a Manager: changes go to the daemon (started on
    demand), reads come straight from the shared state dir."""

    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.version = code_version()
        self._transport = transport
        self.note = ""  # for capabilities: e.g. an older daemon still finishing its jobs

    # -- reads ------------------------------------------------------------------------------

    @property
    def jobs(self) -> dict[str, Job]:
        out: dict[str, Job] = {}
        root = state_dir() / "jobs"
        for p in sorted(root.glob("*/job.json")) if root.is_dir() else []:
            with contextlib.suppress(ValueError, TypeError, OSError):
                job = _settled(Job.load(p))
                out[job.id] = job
        return out

    def get(self, job_id: str) -> Job:
        p = state_dir() / "jobs" / job_id / "job.json"
        if not p.is_file():
            raise JobError(f"no job {job_id!r}")
        return _settled(Job.load(p))

    # -- changes ----------------------------------------------------------------------------

    async def start(
        self,
        task: str,
        done_when: str,
        repo: Path,
        *,
        model: str | None,
        test_command: str | None,
        include_uncommitted: bool,
        time_limit_s: float | None,
        sandbox_mode: str | None,
        kind: str = "change",
        depth: int | None = None,
    ) -> Job:
        body = {
            "task": task,
            "done_when": done_when,
            "repo": str(repo.expanduser().resolve()),  # the daemon has no working directory
            "model": model,
            "test_command": test_command,
            "include_uncommitted": include_uncommitted,
            "time_limit_s": time_limit_s,
            "sandbox_mode": sandbox_mode,
            "kind": kind,
            "depth": depth,
        }
        return job_from(await self._call("POST", "/jobs", body, timeout=180))

    async def revise(self, job_id: str, feedback: str, time_limit_s: float | None) -> Job:
        body = {"feedback": feedback, "time_limit_s": time_limit_s}
        return job_from(await self._call("POST", f"/jobs/{job_id}/revise", body, timeout=180))

    async def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.state not in ACTIVE:
            return job
        return job_from(await self._call("POST", f"/jobs/{job_id}/cancel", None, timeout=90))

    async def discard(self, job_id: str) -> None:
        self.get(job_id)  # a clear error for an unknown id, without starting a daemon
        await self._call("DELETE", f"/jobs/{job_id}", None, timeout=90)

    async def aclose(self) -> None:
        pass  # the daemon outlives us; that's the point

    # -- the daemon -------------------------------------------------------------------------

    def _http(self, timeout: float) -> httpx.AsyncClient:
        transport = self._transport or httpx.AsyncHTTPTransport(uds=str(socket_path()))
        return httpx.AsyncClient(base_url="http://daemon", transport=transport, timeout=timeout)

    async def health(self) -> dict[str, Any] | None:
        try:
            async with self._http(5) as c:
                r = await c.get("/health")
                return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    async def ensure(self) -> dict[str, Any]:
        """A daemon running this code (or an older, busy one finishing its jobs)."""
        h = await self.health()
        if h and h.get("version") != self.version:
            if h.get("running"):
                if not h.get("exit_when_idle"):
                    await self._post_shutdown("idle")
                self.note = (
                    f"an older ai-delegate daemon (pid {h['pid']}, code {h['version']}) is "
                    f"finishing {len(h['running'])} job(s) and still takes new ones; it is "
                    "replaced once idle"
                )
                return h
            await self._post_shutdown("now")
            await self._wait_gone()
            h = None
        self.note = ""
        if h:
            return h
        self._spawn()
        deadline = time.monotonic() + CONNECT_WAIT_S
        while time.monotonic() < deadline:
            if h := await self.health():
                return h
            await asyncio.sleep(0.2)
        raise DaemonError(
            f"the ai-delegate daemon didn't start within {CONNECT_WAIT_S:.0f} s; see {log_path()}"
        )

    async def _post_shutdown(self, when: str) -> None:
        with contextlib.suppress(httpx.HTTPError):
            async with self._http(10) as c:
                await c.post("/shutdown", json={"when": when})

    async def _wait_gone(self, wait_s: float = 30) -> None:
        """Until the old daemon stops answering (it releases the lock as it exits)."""
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if await self.health() is None:
                return
            await asyncio.sleep(0.2)

    def _spawn(self) -> None:
        state_dir().mkdir(parents=True, exist_ok=True)
        with log_path().open("a") as log:
            subprocess.Popen(
                [sys.executable, "-m", "ai_delegate", "daemon"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,  # not in the session's process group: it outlives it
                cwd=state_dir(),
                env=os.environ.copy(),
            )

    async def _call(
        self, method: str, path: str, body: dict[str, Any] | None, timeout: float
    ) -> dict[str, Any]:
        await self.ensure()
        try:
            async with self._http(timeout) as c:
                r = await c.request(method, path, json=body)
        except httpx.HTTPError as e:
            raise DaemonError(f"the ai-delegate daemon didn't answer: {e}") from e
        try:
            doc: dict[str, Any] = r.json()
        except ValueError as e:
            raise DaemonError(f"the daemon answered {r.status_code}: {r.text[:300]}") from e
        if r.status_code >= 400:
            raise JobError(str(doc.get("error") or f"daemon error {r.status_code}"))
        return doc


def _settled(job: Job) -> Job:
    """A job left active by a daemon that has since died: say so (the next daemon to start
    records it the same way)."""
    if job.state in ACTIVE and not jobs_mod._alive(job.pid):
        job.state = "failed"
        job.error = job.error or "interrupted: the ai-delegate process running it ended"
    return job


def status_line() -> str:
    """For doctor: whether a daemon is up, synchronously."""
    if not socket_path().exists():
        return "not running (starts with the first job)"
    try:
        transport = httpx.HTTPTransport(uds=str(socket_path()))
        with httpx.Client(base_url="http://daemon", transport=transport, timeout=5) as c:
            h = c.get("/health").json()
    except (httpx.HTTPError, ValueError):
        return f"not answering (stale socket {socket_path()})"
    same = "" if h["version"] == code_version() else f", older code {h['version']}"
    return f"running, pid {h['pid']}, {len(h['running'])} job(s){same}"
