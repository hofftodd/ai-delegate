"""The job daemon: its socket API, version handover, idle exit and the real subprocess."""

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from test_jobs import env, wait  # noqa: F401 - env is a fixture

from ai_delegate import daemon, jobs


@pytest.fixture
def short_dir():
    """Unix socket paths must be short (about 104 bytes on macOS): pytest's tmp_path isn't."""
    d = Path(tempfile.mkdtemp(prefix="aid-", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@contextlib.asynccontextmanager
async def running_daemon(manager, sock: Path, **daemon_kw):
    d = daemon.Daemon(manager.cfg, manager)
    for k, v in daemon_kw.items():
        setattr(d, k, v)
    task = asyncio.create_task(d.serve(sock))
    for _ in range(200):
        if d._server is not None and d._server.started:
            break
        await asyncio.sleep(0.02)
    try:
        yield d, task
    finally:
        d._stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 20)


def client_for(manager, sock: Path) -> daemon.Client:
    return daemon.Client(manager.cfg, transport=httpx.AsyncHTTPTransport(uds=str(sock)))


async def wait_client(c: daemon.Client, job_id: str) -> jobs.Job:
    for _ in range(300):
        j = c.get(job_id)
        if j.state not in jobs.ACTIVE:
            return j
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


async def test_jobs_through_the_daemon(env, short_dir):  # noqa: F811
    m, repo, _ = env
    sock = short_dir / "d.sock"
    async with running_daemon(m, sock) as (d, _):
        assert oct(sock.stat().st_mode & 0o777) == "0o600"
        c = client_for(m, sock)
        job = await c.start(
            "write hello", "out.txt exists", repo, model=None,
            test_command="test -f out.txt && echo found", include_uncommitted=True,
            time_limit_s=60, sandbox_mode="local",
        )  # fmt: skip
        assert job.pid == os.getpid()  # the daemon's (in-process here)
        done = await wait_client(c, job.id)
        assert done.state == "done", done.error
        assert "+hello" in (done.dir / "result.patch").read_text()  # read straight from disk
        assert job.id in c.jobs

        revised = await c.revise(job.id, "also say revised", 60)
        assert revised.attempt == 2
        done = await wait_client(c, job.id)
        assert "revised" in (done.dir / "result.patch").read_text()

        with pytest.raises(jobs.JobError, match="sandbox must be one of"):
            await c.start(
                "x", "y", repo, model=None, test_command=None, include_uncommitted=True,
                time_limit_s=60, sandbox_mode="vm",
            )  # fmt: skip
        with pytest.raises(jobs.JobError, match="no job"):
            await c.discard("nope")

        await c.discard(job.id)
        assert not done.dir.exists() and job.id not in c.jobs


async def test_cancel_through_the_daemon(env, short_dir):  # noqa: F811
    m, repo, _ = env
    sock = short_dir / "d.sock"
    async with running_daemon(m, sock):
        c = client_for(m, sock)
        job = await c.start(
            "write hello SLOW", "out.txt exists", repo, model=None, test_command=None,
            include_uncommitted=True, time_limit_s=60, sandbox_mode="local",
        )  # fmt: skip
        for _ in range(100):
            if c.get(job.id).state == "running":
                break
            await asyncio.sleep(0.05)
        stopped = await c.cancel(job.id)
        assert stopped.state not in jobs.ACTIVE
        assert "cancelled" in (stopped.stop_reason or "")


async def test_older_idle_daemon_is_replaced(env, short_dir):  # noqa: F811
    m, _, _ = env
    sock = short_dir / "d.sock"
    async with running_daemon(m, sock, version="old000000000") as (_, task):
        c = client_for(m, sock)
        spawned: list[bool] = []

        def fake_spawn():
            spawned.append(True)

        c._spawn = fake_spawn  # type: ignore[method-assign]
        with pytest.raises(daemon.DaemonError, match="didn't start"):
            await asyncio.wait_for(_ensure_quick(c), 30)
        assert spawned == [True]
        await asyncio.wait_for(task, 10)  # the old one was told to stop now, and did


async def _ensure_quick(c: daemon.Client):
    old = daemon.CONNECT_WAIT_S
    daemon.CONNECT_WAIT_S = 1.0
    try:
        await c.ensure()
    finally:
        daemon.CONNECT_WAIT_S = old


async def test_older_busy_daemon_keeps_serving(env, short_dir):  # noqa: F811
    m, repo, _ = env
    sock = short_dir / "d.sock"
    async with running_daemon(m, sock) as (d, _):
        c = client_for(m, sock)
        job = await c.start(
            "write hello SLOW", "x", repo, model=None, test_command=None,
            include_uncommitted=True, time_limit_s=60, sandbox_mode="local",
        )  # fmt: skip
        d.version = "old000000000"  # as if this session runs newer code
        h = await c.ensure()
        assert h["version"] == "old000000000" and d.exit_when_idle
        assert "older ai-delegate daemon" in c.note and "finishing 1 job" in c.note
        assert not d.should_exit()  # busy
        await c.cancel(job.id)
        assert d.should_exit()  # idle and told to exit when idle


def test_idle_exit_rules(env):  # noqa: F811
    m, _, _ = env
    d = daemon.Daemon(m.cfg, m)
    m.cfg.jobs.daemon_idle_exit_s = 0
    d.last_request = time.monotonic() - 10_000
    assert not d.should_exit()  # 0: never exits on its own
    m.cfg.jobs.daemon_idle_exit_s = 60
    assert d.should_exit()
    d.last_request = time.monotonic()
    assert not d.should_exit()
    d.exit_when_idle = True
    assert d.should_exit()


def test_job_of_a_dead_daemon_reads_as_interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "STATE_DIR", tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    job = jobs.Job(id="j1", task="t", done_when="d", repo="/r", state="running", pid=dead.pid)
    job.save()
    c = daemon.Client(daemon.Config())
    got = c.get("j1")
    assert got.state == "failed" and "interrupted" in got.error
    assert c.jobs["j1"].state == "failed"


def test_version_tracks_the_source():
    assert len(daemon.code_version()) == 12 and daemon.code_version() == daemon.code_version()


async def test_real_daemon_subprocess(short_dir):
    """`python -m ai_delegate daemon` for real: one wins the lock, a second exits at once,
    the socket and state dir are private, and shutdown removes the socket."""
    cfg_path = short_dir / "config.yaml"
    cfg_path.write_text("endpoint: http://127.0.0.1:9\n")
    envv = {**os.environ, "AI_DELEGATE_STATE": str(short_dir / "s"),
            "AI_DELEGATE_CONFIG": str(cfg_path)}  # fmt: skip
    envv.pop("AI_DELEGATE_ENDPOINT", None)
    cmd = [sys.executable, "-m", "ai_delegate", "daemon"]
    first = subprocess.Popen(cmd, env=envv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    sock = short_dir / "s" / "daemon.sock"
    try:
        for _ in range(300):
            if sock.exists():
                break
            await asyncio.sleep(0.05)
        assert sock.exists(), first.stdout.read1().decode() if first.stdout else ""
        second = subprocess.run(cmd, env=envv, capture_output=True, text=True, timeout=30)
        assert second.returncode == 0 and "another ai-delegate daemon" in second.stderr
        assert oct((short_dir / "s").stat().st_mode & 0o777) == "0o700"

        transport = httpx.AsyncHTTPTransport(uds=str(sock))
        async with httpx.AsyncClient(base_url="http://d", transport=transport) as c:
            h = (await c.get("/health")).json()
            assert h["pid"] == first.pid and h["running"] == []
            assert h["version"] == daemon.code_version()
            r = await c.post("/shutdown", json={"when": "now"})
            assert r.json() == {"stopping": True}
        assert first.wait(timeout=20) == 0
        assert not sock.exists()
        assert json.loads("{}") == {}
    finally:
        if first.poll() is None:
            first.send_signal(signal.SIGTERM)
            first.wait(timeout=10)
