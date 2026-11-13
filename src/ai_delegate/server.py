"""The MCP server: models, ask and review over stdio."""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
import re
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from ai_delegate import config as config_mod
from ai_delegate import jobs as jobs_mod
from ai_delegate import outcomes
from ai_delegate.routing import Choice, NoModel, pick
from ai_delegate.swap import AiSwap, Cluster, Model, Reply, SwapError

INSTRUCTIONS = """\
ai-delegate gives you local models on the LAN (through ai-swap): free, private, slower and
less capable than you. Part of your goal is to save your own tokens by handing them the work
they can do: reading, searching, reviewing, and bounded edits. Your context is the scarce
resource; theirs is free.

Plan around them: call `capabilities` once at the start of a task to see what is loaded and
how long each tool takes, then decide what to hand off before you start reading. Start long
jobs (`research`, `delegate`) first and keep working while they run. Use `ask` instead of
reading a long file yourself, `research` instead of exploring an unfamiliar codebase,
`review` on your diff before calling work done. Pass paths and diff ranges, not content: the
server reads them, which is where the saving is. Keep for yourself design decisions and
anything that needs this conversation's context; the local models see only what you send.

Treat every answer as a draft to verify. When you know what happened to a result, record it
with `feedback` (kept, fixed, redone, unused): that is how delegation's value is measured.
"""

REVIEW_SYSTEM = """\
You are a careful code reviewer. Report only real problems: bugs, wrong behaviour, unhandled
cases, security issues, and code that does not do what its name or comment says. Skip style
and formatting unless asked. For each finding write one list item:

- [high|medium|low] path:line - what is wrong, and a concrete case where it goes wrong.

Most severe first. If you find nothing worth reporting, answer exactly "No findings." Do not
summarise the change and do not praise it.
"""

logging.getLogger("httpx").setLevel(logging.WARNING)
cfg = config_mod.load()
swap = AiSwap(cfg.endpoint)
manager = jobs_mod.Manager(cfg, swap)
mcp = MCPServer("ai-delegate", instructions=INSTRUCTIONS)


_ITEM_IN_RESULT = re.compile(r"\bid (c-[0-9a-f]{6})\b|Started (?:research )?job (\S+?):")
_session_charged = False


type Tool[**P] = Callable[P, Coroutine[Any, Any, str]]


def metered[**P](fn: Tool[P]) -> Tool[P]:
    """Charge what a tool call puts into the caller's context (its arguments and its result)
    to the item it concerns: the job or call id, else this session's overhead. The first
    call of a session also charges the tool definitions and instructions it carries."""

    sig = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
        global _session_charged
        session = f"session-{os.getpid()}"
        if not _session_charged:
            _session_charged = True
            defs = [t.model_dump(mode="json", exclude_none=True) for t in await mcp.list_tools()]
            outcomes.charge(session, "overhead", outcomes.tokens(json.dumps(defs) + INSTRUCTIONS))
        result = await fn(*args, **kwargs)
        bound = sig.bind_partial(*args, **kwargs).arguments
        shown = {k: v for k, v in bound.items() if k != "ctx" and v is not None}
        item = shown.get("job_id") or shown.get("id")
        if not item and (m := _ITEM_IN_RESULT.search(result)):
            item = m.group(1) or m.group(2)
        cost = outcomes.tokens(json.dumps(shown, default=str)) + outcomes.tokens(result)
        outcomes.charge(str(item or session), fn.__name__ if item else "overhead", cost)
        return result

    return wrapper


class InputError(Exception):
    pass


# -- inputs ---------------------------------------------------------------------------------


def read_files(paths: list[str], base: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in paths:
        path = (base / p).expanduser()
        if not path.is_file():
            raise InputError(f"no such file: {p}")
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            raise InputError(f"{p} looks binary; not sent")
        out.append((p, data.decode("utf-8", errors="replace")))
    return out


async def git_diff(repo: Path, spec: str) -> str:
    args = ["git", "-C", str(repo), "diff", "--no-color", "--no-ext-diff", *spec.split()]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise InputError(f"git diff {spec} failed: {err.decode(errors='replace').strip()}")
    return out.decode("utf-8", errors="replace")


def attach(prompt: str, files: list[tuple[str, str]], diff: str | None) -> str:
    parts = [prompt.strip()]
    for name, text in files:
        parts.append(f"<file path={name!r}>\n{text}\n</file>")
    if diff is not None:
        parts.append(f"<diff>\n{diff}\n</diff>")
    body = "\n\n".join(parts)
    size = len(body.encode())
    if size > cfg.max_input_bytes:
        raise InputError(
            f"input is {size:,} bytes, over the {cfg.max_input_bytes:,}-byte limit "
            "(max_input_bytes in the config); send fewer files or a narrower diff"
        )
    return body


# -- running a call -------------------------------------------------------------------------


async def run(
    tool: str,
    system: str | None,
    user: str,
    model: str | None,
    temperature: float | None,
    timeout_s: float | None,
    ctx: Context,
    native: tuple[int, str] = (0, ""),
) -> str:
    """native: (tokens, basis), what the caller would have read to do this itself."""
    try:
        cluster = await swap.cluster()
        choice = pick(cluster, cfg.routes.get(tool, []), model)
    except (SwapError, NoModel) as e:
        return f"Error: {e}"
    m = choice.model
    approx_in = len(user.encode()) // 3 + (len(system) // 3 if system else 0)
    if m.ctx and approx_in > m.ctx * 3 // 4:
        return (
            f"Error: about {approx_in:,} tokens of input is too much for {m.id}'s "
            f"{m.ctx:,}-token context; send less, or name a model with a larger context"
        )
    budget = min(cfg.max_output_tokens, m.ctx // 4) if m.ctx else min(cfg.max_output_tokens, 16_384)
    deadline = timeout_s or cfg.timeouts.get(tool, 300.0)

    note = f"{m.id} on {m.machine} ({choice.why})"
    await ctx.report_progress(0, None, f"asking {note}")

    async def progress(elapsed: int, msg: str) -> None:
        await ctx.report_progress(elapsed, None, f"{msg}, {elapsed} s")

    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": user}
    ]
    try:
        reply = await swap.chat(
            m.pinned,
            messages,
            max_tokens=budget,
            temperature=temperature,
            deadline_s=deadline,
            progress=progress,
        )
    except SwapError as e:
        return f"Error from {note}: {e}"
    call_id = outcomes.new_call_id()
    outcomes.log_call(
        call_id,
        tool,
        m.name,
        m.machine,
        seconds=round(reply.seconds, 1),
        tokens_in=reply.prompt_tokens,
        tokens_out=reply.completion_tokens,
        timed_out=reply.timed_out,
    )
    if native[0]:
        outcomes.native(call_id, tool, native[0], native[1])
    return render(reply, choice, budget, deadline, call_id)


def render(reply: Reply, choice: Choice, budget: int, deadline: float, call_id: str = "") -> str:
    m = choice.model
    warn: list[str] = []
    if reply.timed_out:
        warn.append(
            f"Stopped at the {deadline:.0f} s time limit; the answer below is partial. "
            "Pass a larger timeout_s, or a faster model."
        )
    if reply.finish == "length":
        if not reply.text.strip() and reply.reasoning_chars:
            warn.append(
                f"{m.id} used its whole {budget:,}-token output budget reasoning and wrote no "
                "answer. Ask a narrower question or use a non-thinking model."
            )
        else:
            warn.append(f"The answer hit the {budget:,}-token output limit and is cut off.")
    if not reply.text.strip() and not warn:
        warn.append(f"{m.id} returned an empty answer.")

    stats = [f"{reply.seconds:.1f} s"]
    if reply.prompt_tokens is not None and reply.completion_tokens is not None:
        stats.insert(0, f"{reply.prompt_tokens:,} in / {reply.completion_tokens:,} out tokens")
    if reply.tps:
        stats.append(f"{reply.tps:.0f} tok/s")
    footer = f"{m.id} on {m.machine} · {choice.why} · " + " · ".join(stats)
    if call_id:
        footer += f" · id {call_id}"
    if choice.skipped:
        footer += "\nskipped: " + "; ".join(choice.skipped)

    head = "".join(f"> {w}\n" for w in warn)
    return f"{head}{'\n' if head else ''}{reply.text.strip()}\n\n---\n{footer}"


# -- tools ----------------------------------------------------------------------------------

ModelArg = Annotated[
    str | None,
    Field(
        description="A model id, name or alias from the models tool (e.g. 'flint' or "
        "'flint@mini-ubuntu'). Leave empty to let ai-delegate pick, preferring loaded models."
    ),
]
TimeoutArg = Annotated[
    float | None, Field(description="Seconds before giving up and returning a partial answer.")
]
FilesArg = Annotated[
    list[str] | None,
    Field(description="Paths of text files to include, relative to the working directory."),
]


TOOL_USES = {
    "ask": "a question over files you'd otherwise read in full",
    "review": "a second-opinion bug review of a diff or files",
    "research": "an agent explores the repo read-only and reports with checked file:line citations",
    "delegate": "an agent makes a bounded change in a sandboxed clone; you get a diff",
}


def _dur(s: float | None) -> str:
    if s is None:
        return "no runs yet"
    return f"typically {s:.0f} s" if s < 90 else f"typically {s / 60:.0f} min"


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@metered
async def capabilities() -> str:
    """A short briefing on what the local models can do for you right now: which models are
    loaded, which each tool would use and how long it typically takes, how past results came
    out, jobs in flight, and tokens saved so far. Call it once at the start of a task to plan
    what to hand off."""
    try:
        cluster = await swap.cluster(with_speed=True)
    except SwapError as e:
        return f"Error: {e} (the local models are unreachable; work natively)"
    lines = ["# Local capabilities", ""]
    mach = []
    for m in cluster.machines.values():
        if not m.online:
            mach.append(f"{m.name} offline")
        elif m.reserved_by:
            mach.append(f"{m.name} RESERVED by {m.reserved_by} (skipped)")
        elif m.budget_gb:
            mach.append(f"{m.name} {m.free_gb:.0f}/{m.budget_gb:g} GB free")
    lines.append("Machines: " + "; ".join(mach))
    loaded = [
        f"{x.name}@{x.machine}"
        + (f" ({x.tps:.0f} tok/s)" if x.tps else "")
        + (f" [{x.inflight} busy]" if x.inflight else "")
        for x in cluster.models
        if x.loaded
    ]
    lines.append("Loaded now: " + (", ".join(loaded) or "nothing; every call will load a model"))
    lines.append("")
    record = outcomes.track_record()
    for tool, use in TOOL_USES.items():
        try:
            c = pick(cluster, cfg.routes.get(tool) or cfg.routes.get("delegate", []))
            via = f"{c.model.name}@{c.model.machine}"
            when = (
                "loaded"
                if c.load == "loaded"
                else (
                    "loads first (fits free memory)" if c.load == "fits" else "loads first, SWAPS"
                )
            )
        except NoModel as e:
            lines.append(f"- {tool}: unavailable ({e})")
            continue
        r = record.get(tool, {})
        oc = r.get("outcomes") or {}
        rated = sum(oc.values())
        rate = (
            f"; {oc.get('kept', 0) + oc.get('fixed', 0)}/{rated} rated results used"
            if rated
            else ""
        )
        lines.append(f"- {tool}: {use}. Now {via}, {when}; {_dur(r.get('median_s'))}{rate}.")
    running = [j for j in manager.jobs.values() if j.state in jobs_mod.ACTIVE]
    lines += [
        "",
        f"Jobs running: {len(running)} of {cfg.jobs.max_running} allowed"
        + (": " + ", ".join(f"{j.id} ({j.kind})" for j in running) if running else "")
        + f"; sandbox {cfg.jobs.sandbox}.",
    ]
    sv = outcomes.savings().get("all")
    if sv:
        lines.append(
            f"Tokens so far (estimates): saved {sv['net']:,} net, {sv['gross']:,} gross, "
            f"over {sv['items']} items; {sv['rated']} rated."
        )
    lines += [
        "",
        "Plan: start research or delegate jobs first and keep working while they run; use ask "
        "instead of reading long files; review your diff before calling work done; pass "
        "paths, not content; rate each result with feedback.",
    ]
    return "\n".join(lines)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@metered
async def models() -> str:
    """List the local models behind ai-swap: machine, loaded or not, context window, measured
    speed, and which tools use them by default. Loaded models answer at once; others must load
    first (seconds to minutes) and may push out a model someone else is using."""
    try:
        cluster = await swap.cluster(with_speed=True)
    except SwapError as e:
        return f"Error: {e}"
    return format_models(cluster)


def format_models(cluster: Cluster) -> str:
    def roles(m: Model) -> str:
        out = []
        for tool, names in cfg.routes.items():
            for i, n in enumerate(names):
                if m.matches(n):
                    out.append(f"{tool} #{i + 1}")
                    break
        return ", ".join(out)

    def measured(m: Model) -> str:
        sc = cfg.scores.get(f"{m.name}@{m.machine}") or {}
        parts = [
            f"{label} {sc[key]:g}%"
            for key, label in (
                ("lcb", "LCB"),
                ("terminal_x3", "terminal"),
                ("terminal_quick", "t-quick"),
            )
            if key in sc
        ]
        return ", ".join(parts)

    lines = [
        "| model | machine | state | context | GB | tok/s | ai-bench | default for |",
        "|" + "---|" * 8,
    ]
    order = sorted(cluster.models, key=lambda m: (not m.loaded, m.machine, m.id))
    for m in order:
        state = m.state + (" (resident)" if m.resident else "")
        if m.inflight:
            state += f", {m.inflight} in flight"
        ctx = f"{m.ctx // 1024}k" if m.ctx else "?"
        tps = f"{m.tps:.0f}" if m.tps else ""
        lines.append(
            f"| {m.id} | {m.machine} | {state} | {ctx} | {m.memory_gb:g} | {tps} "
            f"| {measured(m)} | {roles(m)} |"
        )
    notes = []
    for mach in cluster.machines.values():
        if not mach.online:
            notes.append(f"- {mach.name}: offline")
        elif mach.reserved_by:
            notes.append(f"- {mach.name}: reserved by {mach.reserved_by}; its models are skipped")
        elif mach.budget_gb:
            notes.append(f"- {mach.name}: {mach.free_gb:.0f} of {mach.budget_gb:g} GB free")
    return "\n".join(lines + ([""] + notes if notes else []))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@metered
async def ask(
    prompt: Annotated[str, Field(description="The question or task, complete in itself.")],
    ctx: Context,
    files: FilesArg = None,
    model: ModelArg = None,
    system: Annotated[str | None, Field(description="Optional system prompt.")] = None,
    temperature: Annotated[float | None, Field(ge=0, le=2)] = None,
    timeout_s: TimeoutArg = None,
) -> str:
    """Ask a local model a self-contained question, optionally with files attached. Returns the
    answer and which model answered. The model sees only the prompt and files, not this
    conversation."""
    try:
        attached = read_files(files or [], Path.cwd())
        user = attach(prompt, attached, None)
    except (InputError, OSError) as e:
        return f"Error: {e}"
    read = sum(outcomes.tokens(text) for _, text in attached)
    native = (read, f"read {len(attached)} file(s)")
    return await run("ask", system, user, model, temperature, timeout_s, ctx, native)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@metered
async def review(
    ctx: Context,
    diff: Annotated[
        str | None,
        Field(
            description="What to review, as `git diff` arguments: '' or 'HEAD' for uncommitted "
            "changes, '--staged', 'main...HEAD', 'HEAD~3', or 'HEAD -- path'. Ignored when "
            "diff_text is given. Defaults to uncommitted changes when no files are given."
        ),
    ] = None,
    files: Annotated[
        list[str] | None,
        Field(description="Files to review whole, or to include as context for the diff."),
    ] = None,
    focus: Annotated[
        str | None, Field(description="What to look for, e.g. 'error handling', 'races'.")
    ] = None,
    diff_text: Annotated[
        str | None, Field(description="A diff to review, when it isn't in a local repo.")
    ] = None,
    repo: Annotated[
        str | None, Field(description="Repository path; defaults to the working directory.")
    ] = None,
    model: ModelArg = None,
    timeout_s: TimeoutArg = None,
) -> str:
    """Have a local model review a change or files for bugs, at low temperature. Returns
    findings as a list, most severe first, or "No findings." Treat findings as leads to check:
    local models report false positives."""
    base = Path(repo).expanduser() if repo else Path.cwd()
    try:
        attached = read_files(files or [], base)
        patch: str | None = diff_text
        if patch is None and (diff is not None or not files):
            patch = await git_diff(base, diff or "HEAD")
            if not patch.strip():
                return f"Nothing to review: `git diff {diff or 'HEAD'}` in {base} is empty."
        what = "Review this change." if patch is not None else "Review these files."
        if patch is not None and attached:
            what += " The files are included for context."
        if focus:
            what += f" Focus on: {focus}."
        user = attach(what, attached, patch)
    except (InputError, OSError) as e:
        return f"Error: {e}"
    read = sum(outcomes.tokens(t) for _, t in attached) + outcomes.tokens(patch or "")
    native = (read, "read the diff" + (f" and {len(attached)} file(s)" if attached else ""))
    return await run("review", REVIEW_SYSTEM, user, model, 0.2, timeout_s, ctx, native)


# -- delegated jobs (M2) --------------------------------------------------------------------

INLINE_DIFF_BYTES = 60_000


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False))
@metered
async def delegate(
    task: Annotated[
        str,
        Field(
            description="The change to make, complete in itself: the agent sees only this, "
            "the done-when criteria and the repo. Name files, functions and conventions."
        ),
    ],
    done_when: Annotated[
        str, Field(description="How the agent (and you) can tell the task is finished.")
    ],
    repo: Annotated[
        str | None, Field(description="Repository path; defaults to the working directory.")
    ] = None,
    test_command: Annotated[
        str | None,
        Field(
            description="A shell command that checks the work (e.g. 'uv run pytest -q "
            "tests/test_x.py'). The agent is told to run it; it runs again after the agent "
            "finishes and its output comes back with the result."
        ),
    ] = None,
    include_uncommitted: Annotated[
        bool,
        Field(description="Start from the working tree as it is now (default), not bare HEAD."),
    ] = True,
    model: ModelArg = None,
    time_limit_min: Annotated[
        float | None, Field(description="Wall-clock limit in minutes (default 30).", gt=0)
    ] = None,
    sandbox: Annotated[
        str | None,
        Field(
            description="'docker' (default): a container with only the job's clone and no "
            "network but its model. 'local': pi runs on this machine in the clone with no "
            "isolation, for tests that need the host's toolchain."
        ),
    ] = None,
) -> str:
    """Hand a bounded coding task to a local agent (pi on a local model). It works in its own
    clone of the repo, never your working tree, and comes back as a diff for you to review
    and apply. Returns a job id at once; local models are slow, so expect minutes to tens of
    minutes. Poll with job_status, collect with job_result. Good for mechanical edits,
    boilerplate and first drafts of tests; not for design work."""
    try:
        job = await manager.start(
            task,
            done_when,
            Path(repo).expanduser() if repo else Path.cwd(),
            model=model,
            test_command=test_command,
            include_uncommitted=include_uncommitted,
            time_limit_s=time_limit_min * 60 if time_limit_min else None,
            sandbox_mode=sandbox,
        )
    except (jobs_mod.JobError, NoModel, SwapError, OSError) as e:
        return f"Error: {e}"
    except Exception as e:  # workspace errors and the like, reported, not raised
        return f"Error: {type(e).__name__}: {e}"
    warn = "" if job.sandbox == "docker" else " UNSANDBOXED (local)."
    return (
        f"Started job {job.id}: {job.model} on {job.machine} ({job.route}), sandbox "
        f"{job.sandbox}.{warn} Limit {job.time_limit_s / 60:.0f} min. "
        f'Check it with job_status("{job.id}").'
    )


def _minutes(s: float) -> str:
    return f"{s / 60:.1f} min" if s >= 60 else f"{s:.0f} s"


def _status_line(j: jobs_mod.Job) -> str:
    end = f" ({j.stop_reason or j.error})" if j.stop_reason or j.error else ""
    kind = "research" if j.kind == "research" else "change"
    attempt = f" · attempt {j.attempt}" if j.attempt > 1 else ""
    return (
        f"{j.id} · {kind} · {j.state}{end}{attempt} · {j.model} on {j.machine} · "
        f"{_minutes(j.elapsed)} · {j.task.strip().splitlines()[0][:70]}"
    )


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@metered
async def job_status(
    job_id: Annotated[str | None, Field(description="A job id; empty lists all jobs.")] = None,
) -> str:
    """A delegated job's state, elapsed time, budget use and the agent's last few actions;
    or, with no id, every job kept on this machine."""
    if not job_id:
        js = sorted(manager.jobs.values(), key=lambda j: j.created, reverse=True)
        return "\n".join(_status_line(j) for j in js) or "No jobs."
    try:
        j = manager.get(job_id)
    except jobs_mod.JobError as e:
        return f"Error: {e}"
    lines = [
        _status_line(j),
        f"model calls {j.requests} / {cfg.jobs.max_requests}, new tokens {j.new_tokens:,} / "
        f"{cfg.jobs.max_tokens:,}, limit {_minutes(j.time_limit_s)}, sandbox {j.sandbox}",
    ]
    if j.recent:
        lines += ["", "Last actions:", *(f"  {r}" for r in j.recent)]
    if j.state not in jobs_mod.ACTIVE:
        lines += ["", f'Finished: collect it with job_result("{j.id}").']
    return "\n".join(lines)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@metered
async def job_result(job_id: str) -> str:
    """A finished job's outcome: the agent's summary, test output, the diff, and whether and
    how it applies to your working tree. Review the diff like any other change; the agent's
    own report is not evidence that the work is right, the tests are."""
    try:
        j = manager.get(job_id)
    except jobs_mod.JobError as e:
        return f"Error: {e}"
    if j.state in jobs_mod.ACTIVE:
        return f"Job {j.id} is still {j.state} ({_minutes(j.elapsed)}). " + (
            f"Last: {j.recent[-1]}" if j.recent else ""
        )
    patch_file = j.dir / "result.patch"
    out = [
        f"# Job {j.id}: {j.state}"
        + (f" ({j.stop_reason or j.error})" if j.stop_reason or j.error else ""),
        f"{j.model} on {j.machine} · {_minutes(j.elapsed)} · {j.requests} model calls · "
        f"{j.tool_calls} tool calls · {j.new_tokens:,} new tokens · sandbox {j.sandbox}",
        f"Started from {j.head[:10]}"
        + (f" plus uncommitted work: {j.carried}." if j.carried != "none" else "."),
    ]
    if j.sandbox == "local":
        out.append("Ran UNSANDBOXED: the agent had this machine's full access.")
    elif j.image:
        out.append(f"Sandbox image: {j.image}.")
    if j.history:
        out.append(f"Attempt {j.attempt}, after revising with: {j.feedback[:300]}")
        for h in j.history:
            how = h.get("end") or h.get("state", "")
            out.append(
                f"  attempt {h['attempt']}: {how}"
                + (f", tests exit {h['test_exit']}" if h.get("test_exit") is not None else "")
            )
    rate = (
        f'\nWhen you have used it (or not), record what happened: feedback("{j.id}", '
        '"kept" | "fixed" | "redone" | "unused").'
    )
    if j.kind == "research":
        out += ["", "## Report", "", j.summary or "(none: the agent wrote no report)"]
        c = j.citations
        if c.get("total"):
            out += [
                "",
                f"## Citations: {c['total']} checked",
                "",
                f"- {c['verified']} verified: the quoted code is at the cited lines",
                f"- {c['unanchored']} exist but quote no code to check (or it wasn't found)",
            ]
            if c.get("moved"):
                out.append(f"- {len(c['moved'])} point at the wrong lines; corrected:")
                out += [f"  - {m}" for m in c["moved"]]
            if c.get("bad"):
                out.append(f"- {len(c['bad'])} don't resolve; distrust the claims they back:")
                out += [f"  - {b}" for b in c["bad"]]
        elif j.summary:
            out += ["", "The report cites no `file:line`; treat it as unverified."]
        out.append(
            f'\nAsk a follow-up in the same clone with job_revise("{j.id}", "<question>").' + rate
        )
        return "\n".join(out)
    out += ["", "## Agent's summary", "", j.summary or "(none: the agent wrote no final message)"]
    if j.test_command:
        verdict = (
            "not run"
            if j.test_exit is None
            else "passed"
            if j.test_exit == 0
            else f"FAILED (exit {j.test_exit})"
        )
        tail = "\n".join(j.test_output.splitlines()[-40:])
        out += ["", f"## Tests: `{j.test_command}` {verdict}", "", "```", tail, "```"]
        if j.test_exit == 127 and j.sandbox == "docker":
            out.append(
                f"Exit 127 means a command is missing from the sandbox image ({j.image}). Add "
                "`.ai-delegate/Dockerfile` (FROM ai-factory/executor-base, plus the toolchain) "
                "to the repo, or rerun with sandbox='local'."
            )
    if not patch_file.is_file() or not patch_file.read_text().strip():
        out += ["", "## Diff", "", "No changes."]
        return "\n".join(out) + rate
    patch = patch_file.read_text()
    problem = await workspace_apply_check(Path(j.repo), patch_file)
    out += ["", f"## Diff: {j.diff_files} file(s), {j.diff_lines} line(s)", "", j.diff_stat, ""]
    if len(patch.encode()) <= INLINE_DIFF_BYTES:
        out += ["```diff", patch.rstrip(), "```"]
    else:
        out.append(f"(diff is {len(patch.encode()):,} bytes; read it from {patch_file})")
    out += ["", "## Applying", ""]
    if problem:
        out.append(
            f"Does NOT apply cleanly to {j.repo} as it is now: {problem}\n"
            f"Try `git -C {j.repo} apply --3way {patch_file}`."
        )
    else:
        out.append(f"Applies cleanly: `git -C {j.repo} apply {patch_file}`")
    out.append(f'The job\'s clone is kept at {j.dir / "repo"} until job_discard("{j.id}").')
    out.append(f'To have the agent fix something: job_revise("{j.id}", "<what to change>").')
    return "\n".join(out) + rate


async def workspace_apply_check(repo: Path, patch_file: Path) -> str:
    from ai_delegate import workspace

    return await workspace.apply_check(repo, patch_file)


@mcp.tool(annotations=ToolAnnotations(destructive_hint=False, open_world_hint=False))
@metered
async def job_cancel(job_id: str) -> str:
    """Stop a running job. What it changed so far is kept and can be collected."""
    try:
        j = await manager.cancel(job_id)
    except jobs_mod.JobError as e:
        return f"Error: {e}"
    return _status_line(j)


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
@metered
async def job_discard(job_id: str) -> str:
    """Delete a job: its clone, log and diff. Stops it first if it is running."""
    try:
        await manager.discard(job_id)
    except jobs_mod.JobError as e:
        return f"Error: {e}"
    return f"Discarded job {job_id}."


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@metered
async def research(
    question: Annotated[
        str,
        Field(
            description="What to find out about the codebase, e.g. 'How does a request get "
            "from the HTTP handler to the database?' or 'What breaks if we drop column X?'"
        ),
    ],
    scope: Annotated[
        str | None,
        Field(description="Where to look or what to ignore, e.g. 'only src/api', 'skip tests'."),
    ] = None,
    repo: Annotated[
        str | None, Field(description="Repository path; defaults to the working directory.")
    ] = None,
    include_uncommitted: Annotated[
        bool, Field(description="Look at the working tree as it is now (default), not HEAD.")
    ] = True,
    model: ModelArg = None,
    time_limit_min: Annotated[
        float | None, Field(description="Wall-clock limit in minutes (default 30).", gt=0)
    ] = None,
    sandbox: Annotated[
        str | None, Field(description="'docker' (default) or 'local'; the agent can only read.")
    ] = None,
) -> str:
    """Have a local agent explore the codebase and answer a question: it finds and reads the
    relevant code itself (read, grep, find, ls only; it can't change or run anything) and
    returns a report citing file:line, each citation checked against the repo. Use it instead
    of reading many files yourself. Returns a job id at once; collect with job_result, ask
    follow-ups with job_revise."""
    try:
        job = await manager.start(
            question,
            scope or "",
            Path(repo).expanduser() if repo else Path.cwd(),
            model=model,
            test_command=None,
            include_uncommitted=include_uncommitted,
            time_limit_s=time_limit_min * 60 if time_limit_min else None,
            sandbox_mode=sandbox,
            kind="research",
        )
    except (jobs_mod.JobError, NoModel, SwapError, OSError) as e:
        return f"Error: {e}"
    except Exception as e:  # workspace errors and the like, reported, not raised
        return f"Error: {type(e).__name__}: {e}"
    return (
        f"Started research job {job.id}: {job.model} on {job.machine} ({job.route}), sandbox "
        f'{job.sandbox}. Check it with job_status("{job.id}").'
    )


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False))
@metered
async def job_revise(
    job_id: str,
    feedback: Annotated[
        str,
        Field(
            description="For a change job: what to fix, specifically ('median([]) must raise "
            "ValueError', 'don't touch config.py'). For a research job: the follow-up "
            "question."
        ),
    ],
    time_limit_min: Annotated[
        float | None, Field(description="Limit for this attempt in minutes.", gt=0)
    ] = None,
) -> str:
    """Send a finished job back to its agent with feedback, in the same clone: a change job
    builds on what it already did (the diff stays cumulative), a research job answers a
    follow-up with its earlier report in hand. Each attempt gets fresh budgets. Returns at
    once; poll job_status, collect with job_result."""
    try:
        job = await manager.revise(
            job_id, feedback, time_limit_min * 60 if time_limit_min else None
        )
    except (jobs_mod.JobError, OSError) as e:
        return f"Error: {e}"
    return f'Revising job {job.id} (attempt {job.attempt}). Check it with job_status("{job.id}").'


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False))
@metered
async def feedback(
    id: Annotated[str, Field(description="A job id, or the call id from an ask or review footer.")],
    outcome: Annotated[
        str,
        Field(
            description="kept (used as is), fixed (used after correcting it), redone (thrown "
            "away and done another way), unused (not needed after all)."
        ),
    ],
    note: Annotated[
        str | None, Field(description="One line on why, especially for fixed or redone.")
    ] = None,
) -> str:
    """Record what happened to a delegated result. This is how the user learns whether
    delegation is worth it, per tool and per model (`ai-delegate stats`); call it once you
    know, for every ask, review, delegate and research result you used or dropped."""
    try:
        rec = outcomes.record(id, outcome, note or "")
    except ValueError as e:
        return f"Error: {e}"
    return f"Recorded: {rec['tool']} {id} on {rec['model']}@{rec['machine']}: {outcome}."


def serve() -> None:
    mcp.run("stdio")
