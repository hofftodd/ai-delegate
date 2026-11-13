"""Delegated jobs: start, poll, collect, revise.

A job runs in the background of the ai-delegate process that started it. It is one of two
kinds:

- **change** (``delegate``): pi edits a clone of the caller's repo; the result is a diff;
- **research** (``research``): pi explores the clone with read-only tools (read, grep, find,
  ls: no edit, write or bash) and the result is a report citing ``file:line``, each citation
  checked against the clone.

Its life:

1. **preparing**: clone the caller's repo (workspace.py), pick a model (routing.py), resolve
   the sandbox image;
2. **running**: pi works in the clone, in the sandbox, its model reached via the gateway;
3. **testing** (change jobs with a test command): the command runs in the same sandbox;
4. **done** / **failed** / **stopped**.

``revise`` sends a finished job back to the agent with feedback: a new *attempt* in the same
clone, so a change job keeps what it did and a research job answers a follow-up with its
earlier report in hand. Each attempt gets fresh budgets; the diff stays cumulative against the
job's base.

Budgets per attempt: wall-clock time, model calls and new tokens (gateway.py), and changed
lines (change jobs, checked every few seconds). Hitting one stops the agent and keeps what it
got.

Everything is kept under ``~/.local/state/ai-delegate/jobs/<id>/`` (job.json, log.txt,
result.patch or report.md, repo/), so a result outlives the session that started it; a job
still running when its ai-delegate process ended is marked interrupted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ai_delegate import citations, outcomes, pi, sandbox, workspace
from ai_delegate.config import Config
from ai_delegate.gateway import Budget, Gateway, Meter
from ai_delegate.routing import pick
from ai_delegate.swap import AiSwap

STATE_DIR = Path(os.environ.get("AI_DELEGATE_STATE", "~/.local/state/ai-delegate")).expanduser()
ACTIVE = ("preparing", "running", "testing")
KINDS = ("change", "research")
POLL_S = 5.0  # how often a running job checks its time limit and changed lines


@dataclass
class Job:
    id: str
    task: str  # the change to make, or the research question
    done_when: str
    repo: str
    kind: str = "change"
    state: str = "preparing"
    model: str = ""
    machine: str = ""
    route: str = ""
    window: int = 131_072
    sandbox: str = "docker"
    image: str = ""
    test_command: str | None = None
    include_uncommitted: bool = True
    time_limit_s: float = 1800
    max_diff_lines: int = 800
    created: float = field(default_factory=time.time)
    started: float | None = None  # this attempt's start
    finished: float | None = None
    attempt: int = 1
    feedback: str = ""  # what this attempt was asked to fix or answer, from job_revise
    history: list[dict[str, Any]] = field(default_factory=list)  # earlier attempts
    stop_reason: str = ""
    error: str = ""
    summary: str = ""  # the agent's final message: a change summary, or the report
    recent: list[str] = field(default_factory=list)
    carried: str = ""
    head: str = ""
    base: str = ""
    requests: int = 0  # all attempts
    new_tokens: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    test_exit: int | None = None
    test_output: str = ""
    diff_stat: str = ""
    diff_lines: int = 0
    diff_files: int = 0
    citations: dict[str, Any] = field(default_factory=dict)  # citations.Result.as_dict()
    pid: int = field(default_factory=os.getpid)

    @property
    def dir(self) -> Path:
        return STATE_DIR / "jobs" / self.id

    @property
    def elapsed(self) -> float:
        start = self.started or self.created
        return (self.finished or time.time()) - start

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "job.json.tmp"
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(self.dir / "job.json")

    @classmethod
    def load(cls, path: Path) -> Job:
        raw: dict[str, Any] = json.loads(path.read_text())
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})


class JobError(Exception):
    pass


class Manager:
    def __init__(self, cfg: Config, swap: AiSwap):
        self.cfg = cfg
        self.swap = swap
        self.jobs: dict[str, Job] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.procs: dict[str, sandbox.Proc] = {}
        self.gateway: Gateway | None = None
        self._relay_port: int | None = None
        self._swept = False
        self._load()

    # -- persistence ------------------------------------------------------------------------

    def _load(self) -> None:
        root = STATE_DIR / "jobs"
        if not root.is_dir():
            return
        for p in sorted(root.glob("*/job.json")):
            try:
                job = Job.load(p)
            except (ValueError, TypeError, OSError):
                continue
            if job.state in ACTIVE and job.pid != os.getpid() and not _alive(job.pid):
                job.state = "failed"
                job.error = "interrupted: the ai-delegate process running it ended"
                job.finished = job.finished or time.time()
                job.save()
            self.jobs[job.id] = job

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            p = STATE_DIR / "jobs" / job_id / "job.json"
            if not p.is_file():
                raise JobError(f"no job {job_id!r}")
            job = self.jobs[job_id] = Job.load(p)
        return job

    # -- starting ---------------------------------------------------------------------------

    def _check_capacity(self) -> None:
        running = [j for j in self.jobs.values() if j.state in ACTIVE and j.pid == os.getpid()]
        if len(running) >= self.cfg.jobs.max_running:
            ids = ", ".join(j.id for j in running)
            raise JobError(
                f"{len(running)} job(s) already running ({ids}); the limit is "
                f"{self.cfg.jobs.max_running} (jobs.max_running). Wait, or cancel one."
            )

    async def _check_sandbox(self, mode: str) -> None:
        if mode not in ("docker", "local"):
            raise JobError(f"sandbox must be 'docker' or 'local', not {mode!r}")
        if mode == "docker":
            if why := await asyncio.to_thread(sandbox.docker_ok):
                raise JobError(
                    f"Docker isn't usable ({why}). Pass sandbox='local' to run "
                    "pi directly in the job's clone, without isolation."
                )
            if not await asyncio.to_thread(sandbox.image_present, self.cfg.jobs.image):
                raise JobError(
                    f"the sandbox image {self.cfg.jobs.image} isn't built here; "
                    "run `ai-delegate image` (or pass sandbox='local')"
                )
        elif not shutil.which("pi"):
            raise JobError(
                "pi isn't on PATH, so it can't run locally (npm i -g "
                "@earendil-works/pi-coding-agent), or use the Docker sandbox"
            )

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
    ) -> Job:
        if kind not in KINDS:
            raise JobError(f"kind must be one of {KINDS}")
        self._check_capacity()
        mode = sandbox_mode or self.cfg.jobs.sandbox
        await self._check_sandbox(mode)
        root = await workspace.repo_root(repo)
        cluster = await self.swap.cluster()
        route = "research" if kind == "research" else "delegate"
        candidates = self.cfg.routes.get(route) or self.cfg.routes.get("delegate", [])
        choice = pick(cluster, candidates, model)

        job = Job(
            id=time.strftime("%m%d-%H%M%S-") + secrets.token_hex(2),
            task=task,
            done_when=done_when,
            repo=str(root),
            kind=kind,
            model=choice.model.id,
            machine=choice.model.machine,
            route=choice.why,
            window=choice.model.ctx or self.cfg.jobs.default_context,
            sandbox=mode,
            test_command=test_command if kind == "change" else None,
            include_uncommitted=include_uncommitted,
            time_limit_s=time_limit_s or self.cfg.jobs.time_limit_s,
            max_diff_lines=self.cfg.jobs.max_diff_lines,
        )
        job.save()
        self.jobs[job.id] = job
        self.tasks[job.id] = asyncio.create_task(self._run(job))
        return job

    async def revise(self, job_id: str, feedback: str, time_limit_s: float | None) -> Job:
        """Run another attempt of a finished job in its clone, told what to fix (a change
        job) or what to answer next (a research job)."""
        job = self.get(job_id)
        if job.state in ACTIVE:
            raise JobError(f"job {job_id} is still {job.state}; wait for it or cancel it")
        if not (job.dir / "repo").is_dir() or not job.base:
            raise JobError(f"job {job_id} has no clone to work in (it failed while preparing)")
        if not feedback.strip():
            raise JobError("feedback is empty: say what to fix or what to answer")
        self._check_capacity()
        await self._check_sandbox(job.sandbox)
        job.history.append(
            {
                "attempt": job.attempt,
                "feedback": job.feedback,
                "state": job.state,
                "end": job.stop_reason or job.error,
                "summary": job.summary,
                "test_exit": job.test_exit,
                "diff_lines": job.diff_lines,
            }
        )
        job.attempt += 1
        job.feedback = feedback.strip()
        job.state, job.stop_reason, job.error = "running", "", ""
        job.test_exit, job.test_output, job.citations = None, "", {}
        job.summary, job.recent, job.finished = "", [], None
        job.started = time.time()
        if time_limit_s:
            job.time_limit_s = time_limit_s
        job.pid = os.getpid()
        job.save()
        self.tasks[job.id] = asyncio.create_task(self._attempt(job))
        return job

    async def _gateway(self) -> Gateway:
        if self.gateway is None:
            self.gateway = Gateway(self.cfg.endpoint)
            await self.gateway.start(sandbox.gateway_bind_host())
        return self.gateway

    # -- running ----------------------------------------------------------------------------

    async def _run(self, job: Job) -> None:
        """Prepare the clone and image, then run the first attempt."""
        try:
            repo = job.dir / "repo"
            prep = await workspace.prepare(Path(job.repo), repo, job.include_uncommitted)
            job.head, job.base, job.carried = prep.head, prep.base, prep.carried
            if job.sandbox == "docker":
                job.image, note = await asyncio.to_thread(
                    sandbox.resolve_image, repo, self.cfg.jobs.image, Path(job.repo).name
                )
                with (job.dir / "log.txt").open("a") as log:
                    log.write(f"# image: {note}\n")
        except Exception as e:  # noqa: BLE001 - a job's failure is reported, never raised
            job.error = f"{type(e).__name__}: {e}"
            job.state, job.finished = "failed", time.time()
            job.save()
            self.tasks.pop(job.id, None)
            return
        await self._attempt(job)

    def _prompt(self, job: Job) -> str:
        sandboxed = job.sandbox == "docker"
        if job.kind == "research":
            previous = job.history[-1]["summary"] if job.history else ""
            return pi.research_briefing(job.task, job.done_when, job.feedback, previous)
        brief = pi.briefing(job.task, job.done_when, job.test_command, sandboxed)
        if job.feedback:
            last = job.history[-1] if job.history else {}
            brief += pi.revision_note(
                job.feedback, job.base, last.get("summary", ""), last.get("test_exit")
            )
        return brief

    async def _attempt(self, job: Job) -> None:
        log = (job.dir / "log.txt").open("a", buffering=1)
        token = ""
        meter: Meter | None = None
        collected = False
        try:
            repo = job.dir / "repo"
            ctl = job.dir / "ctl"
            ctl.mkdir(exist_ok=True)
            gw = await self._gateway()
            meter = Meter(
                model=f"{job.model.split('@', 1)[0]}@{job.machine}",
                budget=Budget(self.cfg.jobs.max_requests, self.cfg.jobs.max_tokens),
                on_trip=lambda why: self._stop(job, why),
            )
            token = gw.register(meter)
            if job.sandbox == "docker":
                if not self._swept:
                    await asyncio.to_thread(sandbox.sweep_stale)
                    self._swept = True
                base = await asyncio.to_thread(sandbox.ensure_relay, self.cfg.jobs.image, gw.port)
                self._relay_port = gw.port
            else:
                base = f"http://127.0.0.1:{gw.port}"
            max_out = min(self.cfg.max_output_tokens, job.window // 4)
            (ctl / "models.json").write_text(
                json.dumps(pi.models_config(f"{base}/g/{token}/v1", job.model, job.window, max_out))
            )
            (ctl / "prompt.md").write_text(self._prompt(job))
            log.write(
                f"# {job.id} attempt {job.attempt} ({job.kind}): {job.model} on {job.machine} "
                f"({job.route}), sandbox {job.sandbox}, base {job.base[:10]}\n"
            )
            if job.feedback:
                log.write(f"# feedback: {job.feedback}\n")

            job.state, job.started = "running", job.started or time.time()
            job.save()
            out = pi.PiOutput()
            tools = pi.READ_TOOLS if job.kind == "research" else pi.TOOLS
            step = sandbox.Step(
                f"ai-delegate-{job.id}-{job.attempt}",
                repo,
                ctl,
                pi.command(job.sandbox == "docker", tools),
                {"AI_DELEGATE_MODEL": job.model},
            )
            code = await self._step(job, step, out, log, meter, check_diff=job.kind == "change")
            job.summary = out.final
            job.tool_calls += out.tool_calls
            if not job.stop_reason:
                failure = out.failure() if code == 0 else f"pi exited with status {code}"
                if failure:
                    job.error = failure

            if job.kind == "research":
                (job.dir / "report.md").write_text(job.summary)
                job.citations = citations.check(job.summary, repo).as_dict()
            else:
                # The diff is the agent's work: taken before the test command can add to it.
                await self._collect(job)
                collected = True
                if job.test_command and not job.stop_reason:
                    job.state = "testing"
                    job.save()
                    tstep = sandbox.Step(
                        f"ai-delegate-{job.id}-{job.attempt}-test",
                        repo,
                        ctl,
                        ["bash", "-c", job.test_command],
                        {},
                    )
                    lines: list[str] = []
                    log.write(f"\n# tests: {job.test_command}\n")
                    job.test_exit = await self._step(
                        job, tstep, pi.PiOutput(), log, None, lines, limit=600
                    )
                    job.test_output = "\n".join(lines[-200:])
        except Exception as e:  # noqa: BLE001 - a job's failure is reported, never raised
            job.error = job.error or f"{type(e).__name__}: {e}"
        finally:
            if token and self.gateway:
                if meter is not None:
                    job.requests += meter.requests
                    job.new_tokens += meter.new_tokens
                    job.tokens_out += meter.tokens_out
                    # Claude doing this itself would read and write about what the agent
                    # did, less pi's own prompt (outcomes.py, token ledger).
                    work = meter.new_tokens - outcomes.PI_OVERHEAD_TOKENS
                    tool = "research" if job.kind == "research" else "delegate"
                    outcomes.native(
                        job.id,
                        tool,
                        work,
                        f"attempt {job.attempt}: the local agent read and wrote "
                        f"~{meter.new_tokens:,} tokens",
                    )
                self.gateway.unregister(token)
            if job.kind == "change" and not collected:
                await self._collect(job)
            self._finish(job, log)

    async def _step(
        self,
        job: Job,
        step: sandbox.Step,
        out: pi.PiOutput,
        log: Any,
        meter: Meter | None,
        capture: list[str] | None = None,
        limit: float | None = None,
        check_diff: bool = False,
    ) -> int:
        if job.sandbox == "docker":
            proc = await sandbox.start_docker(
                step, job.image or self.cfg.jobs.image, self.cfg.jobs.cpus, self.cfg.jobs.memory
            )
        else:
            proc = await sandbox.start_local(step)
        self.procs[job.id] = proc
        start = job.started or time.time()
        deadline = start + job.time_limit_s if limit is None else time.time() + limit

        async def read() -> None:
            last_save = 0.0
            async for line in proc.lines():
                if capture is not None:
                    capture.append(line)
                    log.write(line + "\n")
                    continue
                shown = out.render(line)
                if shown:
                    log.write(shown + "\n")
                now = time.monotonic()
                if now - last_save > 2:
                    last_save = now
                    job.recent = list(out.recent)
                    job.save()

        async def watch() -> None:
            while True:
                await asyncio.sleep(POLL_S)
                if time.time() > deadline:
                    if limit is None:
                        self._stop(job, f"time limit reached ({job.time_limit_s / 60:.0f} min)")
                    else:
                        self._stop(job, f"test time limit reached ({limit:.0f} s)")
                    return
                if check_diff:
                    n = await workspace.changed_lines(step.workspace, job.base)
                    if n > job.max_diff_lines:
                        self._stop(
                            job, f"changed-lines budget exceeded ({n} > {job.max_diff_lines})"
                        )
                        return

        reader = asyncio.create_task(read())
        watcher = asyncio.create_task(watch())
        try:
            await reader
            return await proc.wait()
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            await proc.kill()
            self.procs.pop(job.id, None)
            job.recent = list(out.recent)

    def _stop(self, job: Job, reason: str) -> None:
        if job.stop_reason:
            return
        job.stop_reason = reason
        proc = self.procs.get(job.id)
        if proc:
            asyncio.get_running_loop().create_task(proc.kill())

    async def _collect(self, job: Job) -> None:
        repo = job.dir / "repo"
        if not (job.base and repo.is_dir()):
            return
        try:
            change = await workspace.collect(repo, job.base)
            (job.dir / "result.patch").write_text(change.patch)
            job.diff_stat, job.diff_lines, job.diff_files = (
                change.stat,
                change.lines,
                change.files,
            )
        except workspace.WorkspaceError as e:
            job.error = job.error or str(e)

    def _finish(self, job: Job, log: Any) -> None:
        if job.stop_reason:
            job.state = "stopped"
        elif job.error:
            job.state = "failed"
        else:
            job.state = "done"
        job.finished = time.time()
        log.write(f"\n# {job.state}: {job.stop_reason or job.error or 'finished'}\n")
        log.close()
        job.save()
        self.tasks.pop(job.id, None)

    # -- control ----------------------------------------------------------------------------

    async def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.state not in ACTIVE:
            return job
        if job.pid != os.getpid():
            raise JobError(
                f"job {job_id} belongs to another ai-delegate process "
                f"(pid {job.pid}); cancel it from that session"
            )
        self._stop(job, "cancelled by the caller")
        task = self.tasks.get(job.id)
        if task:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), 30)
        return job

    async def discard(self, job_id: str) -> None:
        job = self.get(job_id)
        if job.state in ACTIVE:
            await self.cancel(job_id)
        shutil.rmtree(job.dir, ignore_errors=True)
        self.jobs.pop(job_id, None)

    async def aclose(self) -> None:
        for job_id in list(self.tasks):
            with contextlib.suppress(Exception):
                await self.cancel(job_id)
        if self._relay_port:
            await asyncio.to_thread(sandbox.remove_relay, self._relay_port)
        if self.gateway:
            await self.gateway.aclose()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
