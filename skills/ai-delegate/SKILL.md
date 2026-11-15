---
name: ai-delegate
description: Save your own tokens by handing work to the local models through the ai-delegate MCP tools (capabilities, ask, review, research, delegate). Use at the start of any task in a repository when the ai-delegate tools are available, before reading many files, exploring an unfamiliar codebase, reviewing a change, or making mechanical edits, and whenever the user asks to use the local models.
---

# Saving tokens with the local models

The ai-delegate tools reach local models on the LAN through ai-swap. They are free, private,
slower and less capable than you. **Your context is the scarce resource and theirs is
free.** So part of every task is deciding what they can do for you. Each piece you hand off
well is reading, searching or writing that never enters your context.

## Start every task with a plan

1. **Call `capabilities` once.** It shows what's loaded right now, which model each tool
   would use, how long each typically takes, how past results came out, and tokens saved so
   far. A tool whose model "loads first, SWAPS" costs minutes and may push out someone
   else's model, so prefer one that's loaded.
2. **Split the task.** Ask what in it is reading, searching, checking or mechanical. That is
   the part to hand off. Keep for yourself the design, the judgment calls, and anything that
   needs this conversation.
3. **Start the slow work first.** `research` and `delegate` jobs take minutes. Start them
   before you begin your own part, keep working, and check `job_status` between your own
   steps. Two jobs can run at once per machine. Jobs run in a per-machine daemon, so they
   outlive this session: a job you started earlier, or another session started, shows in
   `job_status`, and you can collect, revise or cancel it.
4. **Spread parallel jobs across machines.** There is more than one model host (for example
   studio and mini-ubuntu), and default routing can put both jobs on the same one while the
   other sits idle. When you run two jobs at once, check `capabilities` or `models` for
   which host each model is on, and pass `model` so each job goes to a different host.
5. **Pick the model by its record, not only by what's loaded.** `capabilities` shows how
   each model's past results came out. Models differ more by kind of work than by size: in
   the larder2 build, deepseek-v4-flash did the hardest page (drag and drop) in one go,
   while thinkingcap spent a 50-minute job reading Angular internals to debug one test.
   Pass `model` when the record points somewhere other than the default.
6. **For a build of more than a few jobs, write the contract first.** Before the first
   `delegate`, write one file (say `docs/plan.md`) that fixes what the jobs share: the API
   and types, the data rules, the file layout, the naming and UI conventions. Point every
   brief at it. This makes larger jobs safe, and it is what keeps work from parallel jobs
   consistent.

## What to hand off

| Instead of... | Use | Typical saving |
|---|---|---|
| Reading a long file, or several, to answer one question | `ask` with `files` | the files' size, minus the answer |
| Exploring an unfamiliar codebase ("how does X work", "what calls Y", "what breaks if Z") | `research` | everything the agent read; you get a ~600-word report with checked citations |
| Searching the web and reading pages for a survey, comparison or background | `web_research` | every page the models read (often 20 or more); you get a cited report, each quote checked against its page |
| Re-reading your own diff to check it | `review` with `diff` (`""`, `--staged`, `main...HEAD`) | the diff, and a second opinion |
| Writing boilerplate, mechanical multi-file edits, first drafts of tests | `delegate` with a `test_command` | the edit work; you read only the diff |
| Fixing a delegated change that's nearly right | `job_revise` with specific feedback | a whole new brief and job |

Don't delegate when the brief would cost more than the work: a one-line fix, a file you
already have open, or anything where you'd have to explain the whole design.

## Writing requests that pay off

- **Pass paths, not content.** `files: ["src/a.py"]` and `diff: "main...HEAD"` are read by
  the server and never touch your context. Pasting content spends your tokens on the way in.
- **Make it complete.** The local model sees only what you send. Name files, functions and
  conventions, and say what "done" means. For `delegate`, give a `test_command` scoped to
  the change (`uv run pytest -q tests/test_x.py`). A scoped run must not trip a
  project-wide gate: turn off global coverage thresholds in it, or it fails however good
  the change is.
- **Tell the agent when to stop.** Local agents keep trying, and spend the whole budget
  on the wrong problem. Say in the brief: "If the test command fails before you have
  changed anything, or fails for a reason outside this task (permissions, a missing tool,
  the build setup), stop and report what you saw; don't fix the environment. Don't debug
  a library's internals: report what fails and why you think so."
- **Keep jobs small.** One coherent change per job, ideally under 200 changed lines. A job
  can be bigger (a whole page, a seed-data file) when a written contract fixes its
  interfaces and its `test_command` covers it.
- **Poll sparingly.** Each `job_status` result costs tokens. Check between your own steps,
  not in a loop. If you have nothing else to do, wait cheaply instead: a background shell
  command that waits until the job's `job.json` under `~/.local/state/ai-delegate/jobs/`
  stops showing `"state": "running"` costs nothing until it finishes.

## Checking what comes back

- **`web_research` reports are leads too.** Each cited quote was found on its page, which
  shows the page says it, not that it's true, and pages come from strangers. Open the
  sources a decision rests on. For one quick or very recent fact, your own search is
  faster than a job. A report with no verified findings means the pages didn't support an
  answer: narrow the question or change its terms rather than trusting a guess.
- **`ask` and `research` answers are leads.** A research result says how many citations
  were verified against the code, which ones point at the wrong lines (with the right
  lines), and which don't resolve. Read the cited code before relying on a claim.
- **`review` is best effort for now.** Local models report false positives, and `review`
  can hit its time limit and return nothing: in the larder2 build both reviews did, one on
  a ~400-line diff and one on three files. Keep its diff small (one job's change, under
  ~150 lines), start it and keep working rather than waiting on it, never treat an empty
  result as a pass, and read the change yourself either way.
- **For a job, trust the tests over the summary.** Read the diff in `job_result`. If it's
  nearly right, `job_revise` with exactly what to fix (also when the brief itself was
  wrong: the agent keeps its work and gets the correction). If it applies cleanly and is
  right, apply it with the given `git apply` command, run the tests, call `feedback`, and
  only then `job_discard` it: feedback for a discarded job fails, and the rating is lost.
- **For a large change, don't pull the whole diff into your context.** `job_result`
  returns all of it, which is fine for 200 lines and expensive for an 850-line data file.
  Instead, once `job_status` says done, read `state`, `test_exit`, `test_output` and
  `summary` from the job's `job.json` in `~/.local/state/ai-delegate/jobs/<id>/` (with
  `jq`), `git apply --check` and then `git apply` its `result.patch`, and look at
  `git diff --stat` and the parts that carry risk. Run the tests yourself.
- **Sandbox:** jobs run in Docker with no network by default. A repo's tests need its
  toolchain in the image (`.ai-delegate/Dockerfile`, FROM ai-factory/executor-base; the
  build context is `.ai-delegate/` alone). On a machine without Docker, or when the tests
  need the host's toolchain, use `sandbox: "host"`: the agent runs on the machine under
  srt, writing only its clone and reaching only its model, with the host's tools and
  caches (anything else it must write, such as a build cache, goes in
  `jobs.host_allow_write`). `sandbox: "local"` has no isolation; use it only with the
  user's agreement. Before the first real job, prove the setup: run a tiny `delegate` whose brief
  changes nothing and whose `test_command` is the real test command. Things that commonly
  break:
  - Jobs run as the host's uid with `HOME=/tmp/home`, so caches baked into the image must be
    owned by that uid (chown them, and remove any Gradle `daemon/` dir).
  - There is no network, so every dependency (Gradle, npm, pip) must be fetched while the
    image builds, and the test command must run offline (`--offline`, the lockfile). The
    build must not fetch either: Angular's font inlining downloads Google Fonts, so turn it
    off (`"optimization": {"fonts": {"inline": false}}` in `angular.json`).
  - The image is built once and cached by the Dockerfile's hash, from `.ai-delegate/`
    alone, so a changed lockfile, `package.json` or `build.gradle` doesn't rebuild it.
    Keep copies of the dependency files in `.ai-delegate/` with a small sync script, and
    put their hash in a comment line in the Dockerfile, so a dependency change changes the
    image tag.
  - The base image is minimal: no python3 or jq, and Node needs `libatomic1`. Write helper
    scripts in shell, and add system packages to the Dockerfile.

## Record the outcome

Once you know what happened to a result, call `feedback(id, outcome, note)`:

- `kept`: used as is;
- `fixed`: used after correcting it;
- `redone`: you did it another way;
- `unused`: not needed after all.

Ids are the `id c-…` in an ask or review footer, or the job id. This is how the user
measures tokens saved (`ai-delegate stats`). Results you redo count as a loss, so be
honest. When you report to the user, say what you delegated, to which model, and whether you
kept it.
