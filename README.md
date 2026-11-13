# ai-delegate

An MCP server that lets Claude Code, or any MCP client, hand bounded work to the local models
behind [ai-swap](https://github.com/hofftodd/ai-swap):

- quick questions about files, and reviews of a diff;
- codebase research: a local agent explores the repo read-only and reports, with its
  `file:line` citations checked against the code;
- delegated coding jobs that a local agent (pi) works on in a sandboxed copy of the repo and
  hands back as a diff, revisable with feedback.

Its purpose is to **save Claude's tokens**. Its instructions and skill make that part of
Claude's goal, and it keeps a ledger of what each call cost Claude against what doing the
work natively would have cost.

It picks models from ai-bench's measurements and prefers one that is already loaded, so a call
doesn't make a machine swap models. It skips machines that ai-bench has reserved, and every
answer says which model on which machine produced it.

**Status:** M1 (quick tools), M2 (delegated jobs) and M3 (routing from ai-bench, and the
Claude Code skill) are built. See [DESIGN.md](DESIGN.md).

## Tools

| Tool | What it does |
|---|---|
| `capabilities` | A short live briefing for planning a task: what's loaded, which model each tool would use and how long it typically takes, how past results came out, jobs in flight, tokens saved so far |
| `models` | Every model ai-swap offers: machine, loaded or not, context, measured tok/s, ai-bench scores, and which tools use it by default. Also each machine's free memory and reservations |
| `ask` | A self-contained question to a local model, with files attached by path. Returns the answer and a footer: model, machine, why it was picked, tokens, seconds, tok/s |
| `review` | Low-temperature bug review of a git diff (`git diff` arguments: `--staged`, `main...HEAD`), whole files, or a pasted diff. Findings, most severe first, or "No findings." |
| `research` | Starts a read-only research job: a question about the codebase, optional scope. The agent can only read, grep, find and ls. Returns a job id at once |
| `delegate` | Starts a coding job: a task, done-when criteria, an optional test command. Returns a job id at once |
| `job_status` | A job's state, elapsed time, budget use and the agent's last actions; with no id, every job |
| `job_result` | A change job's summary, test output, diff, and whether it applies cleanly to your working tree (with the `git apply` command); a research job's report and its citation check |
| `job_revise` | Sends a finished job back to its agent with feedback, in the same clone: a change job builds on its diff, a research job answers a follow-up. Fresh budgets per attempt |
| `job_cancel`, `job_discard` | Stop a job (its work so far is kept), or delete it |
| `feedback` | Records what happened to a result (an `ask`/`review` call id or a job id): kept, fixed, redone or unused |

The server reads files and runs `git diff` itself, so Claude passes a path or a range
instead of pasting content. This saves Claude's tokens.

## Picking a model

Each tool has an ordered candidate list. `ai-delegate routes --write` generates the lists
from ai-bench's results into `~/.config/ai-delegate/routes.yaml`:

- `ask` ranks by LiveCodeBench among models that generate at least 75 tok/s.
- `review` ranks by LiveCodeBench, then terminal-x3, with a 20 tok/s floor.
- `delegate` ranks by terminal-x3, then terminal-quick, then LiveCodeBench, among models
  scoring at least 75% on one of them and generating at least 30 tok/s.

A score belongs to a model on a machine and engine, so the lists name pinned copies such as
`flint@studio`. Lists in `config.yaml` override the generated ones. Without either, built-in
defaults are used (flint first for `ask` and `delegate`, ThinkingCap first for `review`).

Then, on every call, the candidates are tried in this order:

1. A **loaded** candidate is used, even if it ranks lower than one that isn't loaded.
2. Otherwise, a candidate that **fits in a machine's free memory** is loaded.
3. Otherwise, the top candidate is loaded, and that machine **swaps** out idle models. The
   footer says so.

Models on a machine that someone has **reserved** (`/api/lock`, as ai-bench does) are
skipped, and the footer lists them. Naming a model overrides the ranking, but not a
reservation. Requests are always pinned (`name@machine`) to the copy that was picked.

## Saving tokens, and counting it

The server's instructions (always in Claude's context) and the skill tell Claude to plan
around the local models. It calls `capabilities` at the start of a task, decides what to
hand off before reading, starts long jobs first, and passes paths rather than content. Every
tool call goes into a ledger (`~/.local/state/ai-delegate/tokens.jsonl`, estimated at 4
characters per token):

- **Cost to Claude:** what each call put into Claude's context, its arguments plus its
  result, every `job_status` poll included. Each session is also charged once for the tool
  definitions and instructions themselves (about 3.5k tokens).
- **Native estimate:** what Claude would have read doing the work itself. For `ask` and
  `review`, that's the files and diff the server read. For jobs, it's what the local agent
  read and wrote, less pi's own prompt overhead.
- **Saved** is native minus cost. *Net* counts results rated `redone` or `unused` as pure
  losses; unrated ones count at face value.

`ai-delegate stats` prints outcomes per tool and model, and tokens saved, gross and net, per
tool. The native side is an estimate of what Claude would have read, not a measurement. It
doesn't count the reasoning Claude would also have done, and it can't know whether Claude
would have found a shortcut.

## Research jobs

`research(question, scope)` runs pi with only `read`, `grep`, `find` and `ls`. It can't edit
or run anything. Its final message is a report: an answer, findings with `file:line`
citations, and open questions. Each citation is checked in two steps:

- **Does it resolve?** The file exists (as given, or as the one repo file whose path ends
  with it) and the lines are within it.
- **Is the quoted code there?** The backticked code next to the citation is looked for at
  the cited lines. If found, the citation is *verified*. If found elsewhere in the file, it's
  *moved*, and the result gives the line it's probably really on.

The second check exists because pi's `read` shows no line numbers and models count by eye.
The briefing tells the agent to take line numbers from `grep`. `job_revise` asks a follow-up
in the same clone, with the earlier report in hand.

## Delegated jobs

```
delegate(task, done_when, test_command="uv run pytest -q tests/test_x.py")
  → job id; poll job_status; job_result → summary, tests, diff, "git apply …"
```

- **Your working tree is never touched.** A job works in a local `git clone` of your repo at
  HEAD plus your uncommitted changes (tracked edits and untracked files that aren't
  ignored). Pass `include_uncommitted=false` to start from bare HEAD. The result is one diff
  against that starting point, so it applies to your tree as it was when the job started.
  `job_result` checks whether it still does.
- **The sandbox.** By default pi runs in a Docker container with only the job's clone
  mounted. The container sits on an internal network whose only other member is a relay to
  ai-delegate's gateway, so the agent can reach its model and nothing else: not the LAN,
  ai-swap directly, or the internet. `sandbox="local"` runs pi as a host process in the
  clone instead, with no isolation; results say so.
- **Toolchains.** The base image (`ai-factory/executor-base`) has git, bash, rg, fd and jq,
  but no language runtimes. A repo that needs one adds `.ai-delegate/Dockerfile` (or uses
  ai-factory's `.ai-factory/Dockerfile`) `FROM ai-factory/executor-base:latest`. The job
  builds it once and caches it by the Dockerfile's hash.
- **Budgets** per job, as ai-factory's watchdog: 30 minutes, 80 model calls, 400k *new*
  tokens (prompt the model hadn't seen plus output), 800 changed lines, and three identical
  responses in a row. Hitting one stops the agent and keeps what it got.
- **Tests.** With a `test_command` the agent is told to run it, and it runs again after the
  agent finishes, in the same sandbox. The diff is taken before that run, so test artifacts
  never end up in it (nor do `__pycache__`, `node_modules`, `.venv` and the like).
- **Revising.** `job_revise(id, feedback)` runs another attempt in the same clone. The
  agent is told what the previous attempt did and what to fix. The diff stays cumulative
  against the job's base, and each attempt gets fresh budgets.
- Jobs are kept under `~/.local/state/ai-delegate/jobs/<id>/` (job.json, log.txt,
  result.patch or report.md, the clone) until `job_discard`, so a result outlives the session. A job
  whose ai-delegate process ended is marked interrupted. At most 2 jobs run at once per
  session.

## Install

It runs from its checkout with uv. `./suite update` keeps its environment in sync.

```bash
cd ~/dev/ai-tools/ai-delegate && uv sync
uv run ai-delegate image          # build the sandbox image from ../ai-factory (once)
uv run ai-delegate routes --write # rank models from ../ai-bench's results
uv run ai-delegate skill          # link the Claude Code skill into ~/.claude/skills
uv run ai-delegate doctor         # config, ai-swap, what each tool would pick, Docker

# Register with Claude Code for every project (user scope):
claude mcp add -s user ai-delegate -- uv run --project ~/dev/ai-tools/ai-delegate ai-delegate serve
```

Use `--project`, not `--directory`. Claude Code starts the server in the project's directory,
so relative paths, `git diff` and new jobs refer to the repo you're working in, and
`--directory` would change that.

Configuration is optional: copy [examples/config.yaml](examples/config.yaml) to
`~/.config/ai-delegate/config.yaml`. It covers the endpoint, candidate lists, time and size
limits, job budgets and sandbox, and the ai-bench name map. `AI_DELEGATE_ENDPOINT` overrides
the endpoint. Rerun `routes --write` after new ai-bench results; it prints any models it
couldn't match.

## The skill

[skills/ai-delegate/SKILL.md](skills/ai-delegate/SKILL.md) makes saving tokens part of
Claude's goal. It says to start each task with `capabilities` and a plan for what to hand
off, and it gives a table of what to delegate instead of what. It covers writing briefs that
pay off, checking results, and rating every result with `feedback`.

## From the terminal

```bash
uv run ai-delegate stats           # outcomes and tokens saved
uv run ai-delegate models
uv run ai-delegate ask "What does this module do?" -f src/ai_delegate/routing.py
uv run python scripts/smoke.py "Reply with pong."   # full MCP round trip over stdio
```

## Development

```bash
uv run pytest && uv run ruff check && uv run ruff format --check && uv run mypy src
```

Part of the [ai-tools](https://github.com/hofftodd/ai-tools) suite, alongside
[ai-swap](https://github.com/hofftodd/ai-swap) (serving models),
[ai-factory](https://github.com/hofftodd/ai-factory) (orchestrating coding agents) and
[ai-bench](https://github.com/hofftodd/ai-bench) (measuring models).
