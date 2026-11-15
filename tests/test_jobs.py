"""A whole job in local mode, with a fake pi on PATH and fake ai-swap behind the gateway."""

import asyncio
import json
import os
import stat
import subprocess
import sys

import httpx
import pytest
from conftest import models, status

from ai_delegate import jobs
from ai_delegate.config import Config
from ai_delegate.gateway import Gateway
from ai_delegate.swap import AiSwap

FAKE_PI = r"""#!PYTHON
import json, os, sys, time, urllib.request
cfg = json.load(open(os.path.join(os.environ["PI_CODING_AGENT_DIR"], "models.json")))
p = cfg["providers"]["ai-delegate"]
prompt = sys.argv[-1]
def ev(**e): print(json.dumps(e), flush=True)
req = urllib.request.Request(p["baseUrl"] + "/chat/completions", method="POST",
    data=json.dumps({"model": "whatever",
                     "messages": [{"role": "user", "content": prompt}]}).encode(),
    headers={"content-type": "application/json"})
reply = json.load(urllib.request.urlopen(req))["choices"][0]["message"]["content"]
if "## Question" in prompt:  # research: report on the file, citing it
    ev(type="tool_execution_start", toolName="read", args={"path": "notes.txt"})
    ev(type="message_end", message={"role": "assistant",
       "content": "It says `hello world` (notes.txt:1); also (gone.txt:3)."})
    ev(type="agent_settled")
    sys.exit(0)
ev(type="tool_execution_start", toolName="write", args={"path": "out.txt", "content": reply})
text = reply + ("\nrevised" if "## Revision" in prompt else "")
open("out.txt", "w").write(text + "\n")
if "SLOW" in prompt:
    time.sleep(30)
ev(type="message_end", message={"role": "assistant", "content": "Wrote out.txt."})
ev(type="agent_settled")
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pi = bin_dir / "pi"
    pi.write_text(FAKE_PI.replace("PYTHON", sys.executable))
    pi.chmod(pi.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(jobs, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("AI_DELEGATE_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(jobs, "POLL_S", 0.1)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "notes.txt").write_text("hello world\n")
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "init"]):
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True
        )

    seen: list[dict] = []
    leases: list[str] = []  # "take <model> <owner>" / "release <id>", in order

    def swap_api(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/status":
            return httpx.Response(200, json=status())
        if req.url.path == "/v1/models":
            return httpx.Response(200, json=models())
        if req.url.path == "/api/leases":
            body = json.loads(req.content)
            if "lease_id" not in body:
                leases.append(f"take {body['model']} {body['owner']}")
            return httpx.Response(200, json={"lease_id": "L1", "lease": {"expires": "later"}})
        if req.url.path.startswith("/api/leases/"):
            leases.append(f"release {req.url.path.rsplit('/', 1)[1]}")
            return httpx.Response(200, json={"released": True})
        seen.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 2},
            },
        )

    transport = httpx.MockTransport(swap_api)
    cfg = Config(endpoint="http://swap")
    cfg.jobs.sandbox = "local"
    m = jobs.Manager(cfg, AiSwap("http://swap", transport=transport))

    async def gateway() -> Gateway:
        if m.gateway is None:
            m.gateway = Gateway("http://swap", transport=transport)
            await m.gateway.start("127.0.0.1")
        return m.gateway

    m._gateway = gateway  # type: ignore[method-assign]
    m.leases = leases  # type: ignore[attr-defined]  # for the lease test
    return m, repo, seen


async def wait(m: jobs.Manager, job_id: str) -> jobs.Job:
    for _ in range(200):
        if m.get(job_id).state not in jobs.ACTIVE:
            return m.get(job_id)
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


async def test_job_end_to_end(env):
    m, repo, seen = env
    job = await m.start(
        "write hello",
        "out.txt exists",
        repo,
        model=None,
        test_command="test -f out.txt && echo found",
        include_uncommitted=True,
        time_limit_s=60,
        sandbox_mode=None,
    )
    assert job.model == "flint" and job.sandbox == "local"
    done = await wait(m, job.id)
    try:
        assert done.state == "done", done.error
        assert done.summary == "Wrote out.txt." and done.tool_calls == 1
        assert done.requests == 1 and done.new_tokens == 22
        assert seen[0]["model"] == "flint@studio"  # pinned by the gateway
        assert done.test_exit == 0 and "found" in done.test_output
        patch = (done.dir / "result.patch").read_text()
        assert "+hello" in patch and done.diff_files == 1
        assert not (repo / "out.txt").exists()  # the caller's tree is untouched
        reloaded = jobs.Job.load(done.dir / "job.json")
        assert reloaded.state == "done"
    finally:
        await m.aclose()


async def test_time_limit_stops_and_keeps_work(env):
    m, repo, _ = env
    job = await m.start(
        "SLOW",
        "never",
        repo,
        model=None,
        test_command="echo nope",
        include_uncommitted=False,
        time_limit_s=1.0,
        sandbox_mode=None,
    )
    done = await wait(m, job.id)
    try:
        assert done.state == "stopped" and "time limit reached" in done.stop_reason
        assert done.test_exit is None  # tests are skipped after a stop
        assert "+hello" in (done.dir / "result.patch").read_text()
    finally:
        await m.aclose()


async def test_limits_refused_up_front(env):
    m, repo, _ = env
    with pytest.raises(jobs.JobError, match="sandbox must be"):
        await m.start(
            "x",
            "y",
            repo,
            model=None,
            test_command=None,
            include_uncommitted=True,
            time_limit_s=None,
            sandbox_mode="vm",
        )
    with pytest.raises(Exception, match="not inside a git repository"):
        await m.start(
            "x",
            "y",
            repo.parent,
            model=None,
            test_command=None,
            include_uncommitted=True,
            time_limit_s=None,
            sandbox_mode=None,
        )


def test_interrupted_jobs_marked_on_load(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "STATE_DIR", tmp_path)
    j = jobs.Job(id="x", task="t", done_when="d", repo="/r", state="running", pid=999_999)
    j.save()
    m = jobs.Manager(Config(), AiSwap("http://swap"))
    assert m.get("x").state == "failed" and "interrupted" in m.get("x").error


async def test_revise_builds_on_the_first_attempt(env):
    m, repo, seen = env
    job = await m.start(
        "write hello",
        "out.txt exists",
        repo,
        model=None,
        test_command=None,
        include_uncommitted=True,
        time_limit_s=60,
        sandbox_mode=None,
    )
    await wait(m, job.id)
    with pytest.raises(jobs.JobError, match="feedback is empty"):
        await m.revise(job.id, "  ", None)
    await m.revise(job.id, "add a line saying revised", None)
    done = await wait(m, job.id)
    try:
        assert done.state == "done" and done.attempt == 2
        assert done.history[0]["attempt"] == 1 and done.feedback.startswith("add a line")
        assert "## Revision" in seen[-1]["messages"][0]["content"]
        patch = (done.dir / "result.patch").read_text()
        assert "+hello" in patch and "+revised" in patch  # cumulative against the base
        assert done.requests == 2  # one call per attempt, summed
    finally:
        await m.aclose()


async def test_research_job_reports_and_checks_citations(env):
    m, repo, _ = env
    job = await m.start(
        "What does notes.txt say?",
        "",
        repo,
        model=None,
        test_command=None,
        include_uncommitted=True,
        time_limit_s=60,
        sandbox_mode=None,
        kind="research",
    )
    done = await wait(m, job.id)
    try:
        assert done.state == "done" and done.kind == "research"
        assert (done.dir / "report.md").read_text().startswith("It says")
        assert done.citations["verified"] == 1
        assert done.citations["bad"] == ["gone.txt:3 (no such file)"]
        assert not (done.dir / "result.patch").exists()
    finally:
        await m.aclose()


# -- sandbox "host": pi under srt ------------------------------------------------------------

# Stands in for srt: checks the settings it was given, then runs the command unsandboxed.
FAKE_SRT = r"""#!PYTHON
import json, os, sys
assert sys.argv[1] == "-s" and sys.argv[3] == "-c", sys.argv
cfg = json.load(open(sys.argv[2]))
assert cfg["network"]["allowedDomains"][0].startswith("127.0.0.1:"), cfg
assert os.getcwd() in cfg["filesystem"]["allowWrite"], (os.getcwd(), cfg)
assert "SECRET_TOKEN" not in os.environ  # the parent's environment is not passed on
os.execvp("bash", ["bash", "-c", sys.argv[4]])
"""


def _host(env, tmp_path, monkeypatch, srt: str | None = None):
    m, repo, seen = env
    if srt is None:
        fake = tmp_path / "bin" / "srt"
        fake.write_text(FAKE_SRT.replace("PYTHON", sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        m._host_ok = ""  # the fake can't pass the real preflight on Linux (bwrap, socat)
        srt = str(fake)
    m.cfg.jobs.srt = srt
    monkeypatch.setenv("SECRET_TOKEN", "do-not-pass")
    return m, repo, seen


async def test_host_job_runs_under_srt(env, tmp_path, monkeypatch):
    m, repo, _ = _host(env, tmp_path, monkeypatch)
    job = await m.start(
        "write hello", "out.txt exists", repo, model=None,
        test_command="test -f out.txt && echo found", include_uncommitted=True,
        time_limit_s=60, sandbox_mode="host",
    )  # fmt: skip
    done = await wait(m, job.id)
    try:
        assert done.state == "done", (done.error, (done.dir / "log.txt").read_text())
        assert done.sandbox == "host" and done.test_exit == 0
        assert "+hello" in (done.dir / "result.patch").read_text()
        settings = json.loads((done.dir / "ctl" / "srt.json").read_text())
        assert str(done.dir) in settings["filesystem"]["allowRead"]
        assert any(p.endswith("/.ssh") for p in settings["filesystem"]["denyRead"])
    finally:
        await m.aclose()


async def test_host_refused_when_srt_unusable(env, tmp_path, monkeypatch):
    m, repo, _ = env
    m.cfg.jobs.srt = str(tmp_path / "no-such-srt")
    with pytest.raises(jobs.JobError, match="host sandbox isn't usable.*not installed"):
        await m.start(
            "x", "y", repo, model=None, test_command=None, include_uncommitted=True,
            time_limit_s=60, sandbox_mode="host",
        )  # fmt: skip
    await m.aclose()


@pytest.mark.skipif(not os.environ.get("AI_DELEGATE_SRT"), reason="set AI_DELEGATE_SRT=<srt>")
async def test_host_job_with_real_srt(env, tmp_path, monkeypatch):
    """The real thing: the fake pi reaches the gateway through srt's proxy, writes in the
    clone, and can't write outside it or read ~/.ssh."""
    m, repo, _ = _host(env, tmp_path, monkeypatch, os.environ["AI_DELEGATE_SRT"])
    outside = tmp_path / "outside.txt"
    probe = (
        f"echo x > {outside} 2>/dev/null && echo WROTE-OUTSIDE; "
        "ls ~/.ssh >/dev/null 2>&1 && echo READ-SSH; "
        "curl -s -m 5 -o /dev/null https://example.com && echo REACHED-NET; "
        "test -f out.txt && echo found"
    )
    job = await m.start(
        "write hello", "out.txt exists", repo, model=None, test_command=probe,
        include_uncommitted=True, time_limit_s=120, sandbox_mode="host",
    )  # fmt: skip
    done = await wait_long(m, job.id)
    try:
        log = (done.dir / "log.txt").read_text()
        assert done.state == "done", (done.error, log)
        assert "+hello" in (done.dir / "result.patch").read_text()
        assert "found" in done.test_output, done.test_output
        for leak in ("WROTE-OUTSIDE", "READ-SSH", "REACHED-NET"):
            assert leak not in done.test_output, done.test_output
        assert not outside.exists()
    finally:
        await m.aclose()


async def wait_long(m: jobs.Manager, job_id: str) -> jobs.Job:
    for _ in range(1200):
        if m.get(job_id).state not in jobs.ACTIVE:
            return m.get(job_id)
        await asyncio.sleep(0.1)
    raise AssertionError("job did not finish")


def test_host_settings_and_command(tmp_path):
    from ai_delegate import sandbox

    s = sandbox.host_settings(
        tmp_path / "job", tmp_path / "job" / "repo", 4242, ["~/.cache/x"], ["~/y"]
    )
    fs = s["filesystem"]
    assert s["network"]["allowedDomains"] == ["127.0.0.1:4242"]
    assert fs["allowWrite"][:2] == [str(tmp_path / "job" / "repo"), str(tmp_path / "job" / "tmp")]
    assert fs["allowWrite"][2].endswith("/.cache/x") and not fs["allowWrite"][2].startswith("~")
    assert any(p.endswith("/y") for p in fs["denyRead"])
    cmd = sandbox.host_command("srt", tmp_path / "s.json", tmp_path, ["bash", "-c", "echo 'a b'"])
    assert cmd[:4] == ["srt", "-s", str(tmp_path / "s.json"), "-c"]
    assert cmd[4].startswith("unset NO_PROXY no_proxy; export NODE_USE_ENV_PROXY=1 TMPDIR=")
    assert cmd[4].endswith("; exec bash -c 'echo '\"'\"'a b'\"'\"''")


async def test_cancel_while_preparing_never_starts_the_agent(env):
    m, repo, seen = env
    job = await m.start(
        "write hello SLOW", "x", repo, model=None, test_command=None,
        include_uncommitted=True, time_limit_s=60, sandbox_mode=None,
    )  # fmt: skip
    assert job.state == "preparing"
    t0 = asyncio.get_running_loop().time()
    stopped = await m.cancel(job.id)
    try:
        assert asyncio.get_running_loop().time() - t0 < 10  # not the fake agent's 30 s
        assert stopped.state not in jobs.ACTIVE and "cancelled" in stopped.stop_reason
        assert seen == []  # the agent never made a model call
    finally:
        await m.aclose()


async def test_job_holds_a_lease_on_its_model(env):
    m, repo, _ = env
    job = await m.start(
        "write hello", "out.txt exists", repo, model=None, test_command=None,
        include_uncommitted=True, time_limit_s=60, sandbox_mode=None,
    )  # fmt: skip
    done = await wait(m, job.id)
    try:
        assert done.state == "done", done.error
        assert m.leases == [f"take {done.model.split('@')[0]}@{done.machine} ai-delegate/{job.id}",
                            "release L1"]  # fmt: skip
    finally:
        await m.aclose()
