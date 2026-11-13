"""pi (https://pi.dev), the delegated agent, run the way ai-factory runs it.

pi gets its read, bash, edit and write tools plus its built-in grep, find and ls (capped
output that respects .gitignore; ripgrep and fd in the image). Everything it could discover
on its own is off: sessions, extensions, skills, prompt templates, MCP, AGENTS.md/CLAUDE.md,
project-local config and startup network calls. The gateway is its only provider.

pi exits 0 even when a model call fails, so the outcome is read from its JSON event stream.
Adapted from ai-factory's ``executors/pi.py``.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

PROVIDER = "ai-delegate"
TOOLS = ("read", "bash", "edit", "write", "grep", "find", "ls")
READ_TOOLS = ("read", "grep", "find", "ls")  # research: no way to change or run anything
FLAGS = [
    "--mode", "json",
    "--no-session",
    "--offline",
    "--no-extensions",
    "--no-mcp",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--no-context-files",
    "--no-approve",
]  # fmt: skip


def models_config(base_url: str, model: str, window: int, max_output: int) -> dict[str, Any]:
    return {
        "$schema": "https://pi.dev/schemas/models.schema.json",
        "providers": {
            PROVIDER: {
                "name": "ai-delegate gateway",
                "baseUrl": base_url,
                "api": "openai-completions",
                "apiKey": "ai-delegate",  # the gateway needs none; pi wants one set
                "models": [
                    {"id": model, "name": model, "contextWindow": window, "maxTokens": max_output}
                ],
            }
        },
    }


def briefing(task: str, done_when: str, test_command: str | None, sandboxed: bool) -> str:
    rules = [
        "You are working in a copy of a git repository; the current directory is its root. "
        "What you change comes back to the requester as a diff for review, so:",
        "- Make the change described below, and only that. Keep it small and in the style of "
        "the surrounding code.",
        "- Don't commit, push, rebase or change git config. Don't write outside the repository.",
        "- Use grep, find and ls to explore (capped output, respects .gitignore) rather than "
        "`grep -r` through bash. rg, fd and jq are installed.",
    ]
    if sandboxed:
        rules.append("- There is no network access: don't try to install packages.")
    if test_command:
        rules.append(f"- Check your work by running `{test_command}`; it should pass.")
    rules.append(
        "- When you are done, reply with a short summary: what you changed and why, how you "
        "checked it, and anything you could not do. Say plainly if the task is not finished."
    )
    return (
        "\n".join(rules) + f"\n\n## Task\n\n{task.strip()}\n\n## Done when\n\n{done_when.strip()}\n"
    )


def command(sandboxed: bool, tools: tuple[str, ...] = TOOLS) -> list[str]:
    """The command to start pi. Its agent dir must be writable, so the read-only control
    dir's models.json is copied into a fresh one first."""
    flags = " ".join([*FLAGS, "--tools", ",".join(tools)])
    agent = "$HOME/.pi/agent" if sandboxed else "$AI_DELEGATE_CTL/agent"
    return [
        "bash",
        "-c",
        f'mkdir -p "{agent}" && cp "$AI_DELEGATE_CTL/models.json" "{agent}/" '
        f'&& export PI_CODING_AGENT_DIR="{agent}" '
        f'&& exec pi {flags} --provider {PROVIDER} --model "$AI_DELEGATE_MODEL" -- '
        '"$(cat "$AI_DELEGATE_CTL/prompt.md")"',
    ]


def revision_note(feedback: str, base: str, summary: str, test_exit: int | None) -> str:
    """Appended to the original briefing for a change job's next attempt."""
    if test_exit is None:
        tests = ""
    elif test_exit == 0:
        tests = " Its test command passed."
    else:
        tests = f" Its test command failed (exit {test_exit})."
    prior = f"\n\nIts summary was:\n\n{summary.strip()}" if summary.strip() else ""
    return (
        "\n## Revision\n\n"
        "A previous attempt at this task already made changes; they are in the working tree "
        f"now (`git diff {base[:12]}` shows them, `git status` lists the files).{tests}{prior}"
        "\n\nThe reviewer's feedback on it, which this attempt must address:\n\n"
        f"{feedback.strip()}\n\n"
        "Build on the existing changes: fix what the feedback asks, keep what it doesn't "
        "mention, and say in your summary what you changed in this attempt.\n"
    )


def research_briefing(question: str, scope: str, followup: str, previous: str) -> str:
    rules = [
        "You are answering a question about the code in this repository; the current "
        "directory is its root. You can only read: use grep, find and ls to locate code "
        "(capped output, respects .gitignore) and read to look at it.",
        "- Look before you answer: find the relevant code and read it. Don't guess from names.",
        "- Cite every claim as `path:line` or `path:start-end`, with the path relative to "
        "the repository root, pointing at the lines that show it, and quote the key code in "
        "backticks next to the citation. Citations are checked against the code.",
        "- Line numbers: grep prints them (`path:line:`); read does not, so don't count lines "
        "by eye. Take each line number from grep's output, or from read's offset.",
        "- Say plainly what you could not find or are unsure of.",
        "- Your final message is the report, in this shape: a direct answer first (a few "
        "sentences), then the evidence as a list of findings, each with its citation, then "
        "open questions if any. Keep it under about 600 words.",
    ]
    out = "\n".join(rules) + f"\n\n## Question\n\n{question.strip()}\n"
    if scope.strip():
        out += f"\n## Scope\n\n{scope.strip()}\n"
    if followup:
        out += (
            f"\n## Your earlier report\n\n{previous.strip() or '(none)'}\n"
            f"\n## Follow-up\n\n{followup.strip()}\n\nAnswer the follow-up; reuse "
            "the earlier report's findings where they still hold, and correct any that "
            "were wrong. The final message is the new report, complete in itself.\n"
        )
    return out


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _clip(text: str, lines: int = 12) -> str:
    rows = text.rstrip("\n").splitlines()
    if len(rows) <= lines:
        return "\n".join(rows)
    return "\n".join([*rows[:lines], f"... ({len(rows) - lines} more lines)"])


def describe_call(name: str, args: Any) -> str:
    if not isinstance(args, dict):
        return name
    if name == "bash":
        return f"$ {str(args.get('command', ''))[:300]}"
    path = args.get("path", "")
    if name == "edit":
        return f"edit {path} ({len(args.get('edits') or [])} change(s))"
    if name == "write":
        return f"write {path} ({len(str(args.get('content', '')).splitlines())} lines)"
    if name == "read":
        return f"read {path}"
    return f"{name} {json.dumps(args)[:200]}"


class PiOutput:
    """Turns pi's JSONL events into a readable log, remembers the last few actions and
    the final message, and how the run ended."""

    def __init__(self, keep: int = 12) -> None:
        self.recent: deque[str] = deque(maxlen=keep)
        self.final = ""
        self.tool_calls = 0
        self._error: str | None = None
        self._settled = False

    def render(self, line: str) -> str | None:
        out = self._render(line)
        if out:
            first = out.splitlines()[0] if out.strip() else out
            self.recent.append(first[:300])
        return out

    def _render(self, line: str) -> str | None:
        if not line.startswith("{"):
            return line or None
        try:
            ev = json.loads(line)
        except ValueError:
            return line
        kind = ev.get("type")
        if kind == "message_end":
            msg = ev.get("message") or {}
            if msg.get("role") != "assistant":
                return None
            if msg.get("stopReason") == "error":
                self._error = str(msg.get("errorMessage") or "model call failed")
                return f"model error: {self._error}"
            self._error = None
            text = _text(msg.get("content")).strip()
            if text:
                self.final = text
            return text or None
        if kind == "tool_execution_start":
            self.tool_calls += 1
            return describe_call(str(ev.get("toolName", "")), ev.get("args"))
        if kind == "tool_execution_end":
            result = _text((ev.get("result") or {}).get("content"))
            if ev.get("isError"):
                return f"[{ev.get('toolName')} failed] {_clip(result, 3)}"
            return None
        if kind == "auto_retry_start":
            return f"retrying model call (attempt {ev.get('attempt')}): {ev.get('errorMessage')}"
        if kind == "auto_retry_end" and not ev.get("success"):
            self._error = str(ev.get("finalError") or "model call failed after retries")
            return f"model error: {self._error}"
        if kind == "compaction_start":
            return f"compacting context ({ev.get('reason')})"
        if kind == "agent_settled":
            self._settled = True
            if ev.get("aborted"):
                self._error = self._error or "pi aborted the run"
        return None

    def failure(self) -> str | None:
        if self._error:
            return f"pi: {self._error}"
        if not self._settled:
            return "pi ended without finishing its run"
        return None
