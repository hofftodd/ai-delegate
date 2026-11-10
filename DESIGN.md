# ai-delegate: design

Status: design only, 2026-11-10. Nothing is built yet.

## Purpose and scope

ai-delegate is a planned fifth tool in the suite: an MCP server that lets Claude Code hand bounded work to the local models behind ai-swap, while Claude keeps the planning and the final say. It is a future project; nothing is built yet, and a trial of an existing server (Houtini LM) comes first.

- In scope: short questions and reviews answered by a local model, and delegated coding jobs that run in a throwaway copy of the repo and come back as a diff.
- Out of scope: running Claude Code itself on a local model (`claude-swap` already does that), and multi-task orchestration (that is ai-factory).

## Prior art

Two existing servers cover the two halves of the idea; neither knows anything about how our models are hosted.

| Project | Pattern | What it does | Backends |
| --- | --- | --- | --- |
| [Houtini LM](https://houtini.com/tools/houtini-lm/) (`@houtini/lm`, Node) | Ask | `chat` for delegated questions, `code_task` for low-temperature code review; Claude stays the architect and checks the output | Any OpenAI-compatible endpoint (LM Studio, Ollama, vLLM, llama.cpp, hosted APIs) |
| [local-vibes](https://github.com/OpenSourceWTF/local-vibes) | Delegate | The local model runs its own agent loop with file and shell tools in a working directory, then returns a summary and a git change report; installs as a Claude Code plugin with a skill that says when to delegate | OpenAI-compatible (Ollama by default) |

What the suite adds that they lack:

- **Swap awareness.** ai-swap knows which model is loaded on each machine and when ai-bench has a machine reserved. Swapping a model in takes a minute or more, so routing to a loaded model matters.
- **Measured routing.** ai-bench has per-model scores and speeds, so each tool can default to the model that measured best for that kind of work.
- **A proven agent harness.** ai-factory already runs pi in a sandbox behind a gateway with budgets and loop detection, and the future-lab run shook out its failure modes (see Long-running jobs and limits).

## Tools exposed to Claude Code

Six tools in two groups: quick calls that answer within one MCP request, and delegated jobs that run for minutes and are polled.

| Tool | Group | Takes | Returns |
| --- | --- | --- | --- |
| `models` | Quick | nothing | Each model: machine, loaded or not, context window, measured speed, what ai-bench rates it for |
| `ask` | Quick | A prompt, optional files to include, optional model | The model's answer, the model used, tokens and seconds |
| `review` | Quick | A diff or files, and what to look for | Findings as a list, low temperature |
| `delegate` | Job | A task in plain words, the repo path, done-when criteria, optional model and time limit | A job id at once |
| `job_status` | Job | A job id | State, elapsed time, the agent's last few actions |
| `job_result` | Job | A job id | Summary, the diff, test output if it ran tests, and how to apply the diff |

Claude decides what to delegate; a skill shipped with the server says when delegation pays (mechanical edits, boilerplate, first drafts of tests) and when it doesn't (design, anything needing the whole conversation).

- Open question: whether `review` earns its own tool or is `ask` with a preset prompt.

## Architecture

ai-delegate is a local stdio MCP server, a new repo in the suite, that uses ai-swap as its only way to reach models.

```mermaid
flowchart LR
    subgraph local["Your machine, where Claude Code runs"]
        cc["Claude Code<br/>plans, delegates, reviews, applies the diff"]
        ad["ai-delegate (new)<br/>stdio MCP server: tools, routing, jobs<br/>gateway: budgets, loop stop, no keys inside"]
        sb["Job sandbox, one per delegated job<br/>pi in Docker, in its own git worktree<br/>returns a diff; never edits in place"]
        cc <-->|MCP over stdio| ad
        ad <-->|jobs; pi's model calls| sb
    end
    subgraph lan["Model servers on the LAN"]
        sw["ai-swap (studio.local:8080)<br/>one OpenAI-compatible endpoint<br/>model state and reservations"]
        m["Mac Studio and mini-ubuntu<br/>where the models run"]
        b["ai-bench<br/>scores and speeds per model<br/>reserves a machine while it runs"]
        sw -->|loads and routes| m
        b -->|tests, reserves| m
    end
    ad -->|requests, model state| sw
    b -.->|model lists| ad
```

Claude Code talks only to ai-delegate; every model call, including the sandboxed agent's, goes through ai-delegate's gateway to ai-swap, which picks the machine.

Why a separate project and not part of ai-swap:

- **Delegation needs the working tree.** A job works on the repo Claude is in, so it must run where Claude Code runs. ai-swap runs on the model servers.
- **ai-swap stays a router.** It loads models and routes requests; an agent loop, worktrees and Docker don't belong in it.
- **What ai-swap may gain:** a small, stable status endpoint for routing, if the current one isn't enough. The quick tools could later also be served from ai-swap over HTTP (open question).

## Model routing

A call names a model, or names a kind of work and lets ai-delegate pick; either way the pick avoids making a machine swap models when it can.

1. **Kind of work → candidates.** Each tool has an ordered list of models, taken from ai-bench results: for example fast drafting on flint or nemotron-lightning, careful review on ThinkingCap. The lists live in config and are regenerated from ai-bench, not hand-maintained.
2. **Prefer what is loaded.** ai-swap reports each model's state. A loaded candidate wins over a better-ranked one that would need a swap, unless the call says otherwise.
3. **Respect reservations.** While ai-bench has a machine reserved, its models are skipped; if no candidate is left, the call says so instead of waiting silently.
4. **Report the choice.** Every result names the model and machine used, so Claude (and the user) can see what answered.

This is the same swap-avoidance idea as ai-factory's residency rules, read from the same ai-swap status endpoint.

## Delegation safety

A delegated job never touches the user's working tree: it works in its own git worktree and hands back a diff that Claude decides whether to apply.

- **Worktree per job.** `git worktree add` on a new branch from the current HEAD, including uncommitted changes the caller asks for. Removed when the result is collected or the job is discarded.
- **Diff back, not edits in place.** `job_result` returns the diff and the commands to apply it; Claude reviews it like any other change.
- **Sandbox by default.** The agent (pi) runs in a Docker container with only the worktree mounted, reusing ai-factory's executor image and gateway pattern: the model is reached only through a local gateway that counts calls and tokens and stops loops. A no-Docker mode runs pi directly in the worktree, for machines without Docker, and says so.
- **Budgets.** Per job: wall-clock limit, model calls, new tokens (counted as in ai-factory#18), and changed lines. Hitting one ends the job with what it got so far.
- **No secrets in the sandbox.** Node API keys stay on the host side of the gateway, as in ai-factory.

## Long-running jobs and limits

Local models are slow enough that delegation has to be asynchronous: ThinkingCap on the R9700 writes about 37 tokens a second, and future-lab's tasks took 35 to 100 minutes each.

- **Start, then poll.** `delegate` returns a job id immediately; Claude checks `job_status` and collects `job_result`. Quick tools (`ask`, `review`) keep a short limit and say so when a model is too slow for it.
- **Output budget for thinking models.** Give agents a quarter of the context window per reply, up to 32k tokens; at 8k, ThinkingCap reasoned until it was cut off and wrote nothing (ai-factory#16).
- **Long output lines.** Read agent output in chunks; pi's JSON events can carry a whole file on one line (ai-factory#15).
- **Count new tokens.** Budget the prompt the model hasn't seen plus its output, not every resent prompt (ai-factory#18).
- **Tell the agent its tools.** Give pi its grep, find and ls tools, with rg, fd and jq installed, and say so in the briefing (ai-factory#20).
- **Don't trust a "no problems" note over the tests.** Act on an agent's report only when the tests agree (ai-factory#17).

## Configuration and install

Install is one command per machine that runs Claude Code, and the suite keeps it up to date like the other tools.

- **Register with Claude Code:** `claude mcp add ai-delegate -- ai-delegate serve` (stdio). `ai-swap-clients` could add it alongside the providers it already writes.
- **Config** in `~/.config/ai-delegate/config.yaml`: the ai-swap endpoint (default `http://studio.local:8080`), per-tool model lists (generated from ai-bench), budgets, and sandbox on or off.
- **Suite integration:** a new repo cloned by `./suite clone`, updated by `./suite update`, and listed in the suite README and ARCHITECTURE; `ai-delegate doctor` checks ai-swap is reachable, Docker works, and the image is present.
- **Language:** Python, to share pi and gateway code with ai-factory and results parsing with ai-bench (open question below).

## Evaluation plan

Before building anything, use Houtini LM against ai-swap for a few days of real work, between ai-factory runs, and keep notes on what delegation is worth.

- **Setup:** `claude mcp add houtini-lm -- npx -y @houtini/lm` with `HOUTINI_LM_ENDPOINT_URL=http://studio.local:8080/v1`.
- **Note for each delegation:** what was asked, which model answered, how long it took, whether Claude used the answer as is, fixed it, or redid the work, and whether a model swap happened.
- **Build ai-delegate if** delegated answers are kept more often than redone, and the misses trace to things ai-delegate fixes: swaps, wrong model for the job, or work that needed an agent loop rather than one answer.
- **Don't build it if** Claude ends up redoing most delegated work; then the bottleneck is the models, not the plumbing.
- **Watch during the trial:** that ai-factory runs aren't slowed by swaps the trial causes (the reason to trial between runs).

## Milestones and open questions

Each milestone is usable on its own; the trial decides whether the later ones happen.

1. **Trial:** Houtini LM against ai-swap, with notes (Evaluation plan). Gate: the build criteria are met.
2. **M1, quick tools:** `models`, `ask`, `review` over stdio, with swap-aware routing from ai-swap's status. Replaces Houtini for daily use.
3. **M2, delegation:** `delegate`, `job_status`, `job_result` with a worktree per job and pi in the sandbox; budgets and the diff-back flow.
4. **M3, measured routing and the skill:** model lists generated from ai-bench results, and the Claude Code skill that says when to delegate.

Open questions:

- [ ] Python (shares ai-factory's pi and gateway code) or TypeScript (the MCP ecosystem's default)?
- [ ] Should the quick tools also be served by ai-swap itself as an HTTP MCP endpoint, so any machine can use them without installing ai-delegate?
- [ ] How much of ai-factory's sandbox and gateway can be shared as a library rather than copied?
- [ ] Does a delegated job need the conversation's context, or only the task text and the repo?
- [ ] What MCP tool-call time limit does Claude Code apply, and is it configurable, for `ask` on slow models?
