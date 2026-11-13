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
   steps. Two jobs can run at once.

## What to hand off

| Instead of... | Use | Typical saving |
|---|---|---|
| Reading a long file, or several, to answer one question | `ask` with `files` | the files' size, minus the answer |
| Exploring an unfamiliar codebase ("how does X work", "what calls Y", "what breaks if Z") | `research` | everything the agent read; you get a ~600-word report with checked citations |
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
  the change (`uv run pytest -q tests/test_x.py`).
- **Keep jobs small.** One coherent change per job, ideally under 200 changed lines.
- **Poll sparingly.** Each `job_status` result costs tokens. Check between your own steps,
  not in a loop.

## Checking what comes back

- **`ask` and `research` answers are leads.** A research result says how many citations
  were verified against the code, which ones point at the wrong lines (with the right
  lines), and which don't resolve. Read the cited code before relying on a claim.
- **`review` findings need checking.** Local models report false positives.
- **For a job, trust the tests over the summary.** Read the diff in `job_result`. If it's
  nearly right, `job_revise` with exactly what to fix. If it applies cleanly and is right,
  apply it with the given `git apply` command, run the tests, then `job_discard` it.
- **Sandbox:** jobs run in Docker with no network. A repo's tests need its toolchain in the
  image (`.ai-delegate/Dockerfile`, FROM ai-factory/executor-base). `sandbox: "local"` has no
  isolation; use it only with the user's agreement.

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
