"""What happened to delegated work: the record that says whether delegation pays.

Every ``ask`` and ``review`` call gets an id (``c-…``; jobs use their job id) and a line in
``calls.jsonl``: tool, model, machine, seconds, tokens. ``feedback`` adds a line to
``feedback.jsonl`` saying what the caller did with the result:

- **kept**: used as is;
- **fixed**: used after correcting it;
- **redone**: thrown away and done another way;
- **unused**: not needed after all (says nothing about quality).

``ai-delegate stats`` sums them per tool and per model. This is the evaluation DESIGN.md asks
for before building further, and the data a later routing step could rank models by.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

OUTCOMES = ("kept", "fixed", "redone", "unused")


def state_dir() -> Path:
    return Path(os.environ.get("AI_DELEGATE_STATE", "~/.local/state/ai-delegate")).expanduser()


def _append(name: str, record: dict[str, Any]) -> None:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    with (d / name).open("a") as f:
        f.write(json.dumps(record) + "\n")


def _read(name: str) -> list[dict[str, Any]]:
    p = state_dir() / name
    if not p.is_file():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def new_call_id() -> str:
    return "c-" + secrets.token_hex(3)


def log_call(call_id: str, tool: str, model: str, machine: str, **extra: Any) -> None:
    _append(
        "calls.jsonl",
        {
            "id": call_id,
            "tool": tool,
            "model": model,
            "machine": machine,
            "at": time.time(),
            **extra,
        },
    )


def lookup(item_id: str) -> dict[str, Any] | None:
    """The call or job an id names: {tool, model, machine}."""
    for rec in reversed(_read("calls.jsonl")):
        if rec.get("id") == item_id:
            return rec
    job = state_dir() / "jobs" / item_id / "job.json"
    if job.is_file():
        j = json.loads(job.read_text())
        tool = "research" if j.get("kind") == "research" else "delegate"
        return {
            "id": item_id,
            "tool": tool,
            "model": j.get("model", ""),
            "machine": j.get("machine", ""),
            "attempts": j.get("attempt", 1),
        }
    return None


def record(item_id: str, outcome: str, note: str = "") -> dict[str, Any]:
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
    what = lookup(item_id)
    if what is None:
        raise ValueError(f"no call or job {item_id!r} (ids are in each result's footer)")
    rec = {
        "id": item_id,
        "outcome": outcome,
        "note": note.strip(),
        "at": time.time(),
        "tool": what.get("tool", ""),
        "model": what.get("model", ""),
        "machine": what.get("machine", ""),
    }
    if "attempts" in what:
        rec["attempts"] = what["attempts"]
    _append("feedback.jsonl", rec)
    return rec


def stats() -> str:
    calls = _read("calls.jsonl")
    fb = {r["id"]: r for r in _read("feedback.jsonl") if r.get("id")}  # the last one wins
    jobs = []
    jroot = state_dir() / "jobs"
    if jroot.is_dir():
        for p in jroot.glob("*/job.json"):
            try:
                jobs.append(json.loads(p.read_text()))
            except ValueError:
                continue
    total: dict[tuple[str, str], int] = defaultdict(int)
    for c in calls:
        total[(c.get("tool", ""), f"{c.get('model', '')}@{c.get('machine', '')}")] += 1
    for j in jobs:
        tool = "research" if j.get("kind") == "research" else "delegate"
        total[(tool, f"{str(j.get('model', '')).split('@')[0]}@{j.get('machine', '')}")] += 1
    for r in fb.values():  # discarded jobs still count through their feedback
        key = (r.get("tool", ""), f"{str(r.get('model', '')).split('@')[0]}@{r.get('machine', '')}")
        total.setdefault(key, 1)

    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in fb.values():
        key = (r.get("tool", ""), f"{str(r.get('model', '')).split('@')[0]}@{r.get('machine', '')}")
        counts[key][r["outcome"]] += 1
    if not total:
        return "No delegated work recorded yet."
    rows = [
        "| tool | model | uses | rated | kept | fixed | redone | unused |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for key in sorted(total):
        c = counts.get(key, {})
        rated = sum(c.values())
        rows.append(
            f"| {key[0]} | {key[1]} | {total[key]} | {rated} | {c.get('kept', 0)} | "
            f"{c.get('fixed', 0)} | {c.get('redone', 0)} | {c.get('unused', 0)} |"
        )
    judged = [r for r in fb.values() if r["outcome"] != "unused"]
    if judged:
        good = sum(r["outcome"] in ("kept", "fixed") for r in judged)
        redone = sum(r["outcome"] == "redone" for r in judged)
        rows += [
            "",
            f"Of {len(judged)} rated results, {good} were used (kept or fixed) and "
            f"{redone} redone. DESIGN.md's bar for building further: used more often "
            "than redone.",
        ]
    return "\n".join(rows)


# -- token ledger -----------------------------------------------------------------------------
#
# Estimates, in Claude tokens (about 4 characters each), of what delegation cost Claude and
# what doing the work natively would have cost it:
#
# - **cost**: what each ai-delegate tool call put into Claude's context, its arguments plus
#   its result, polls included; and once per session, the tool definitions themselves.
# - **native**: what Claude would have read doing it itself: the files and diff an ask or
#   review read for it; for a job, what the local agent read and wrote (its new tokens, less
#   pi's own prompt).
#
# Saved = native - cost. Net counts a result rated "redone" or "unused" as a pure loss (the
# native work happened anyway, or wasn't needed); unrated results count at face value.

CHARS_PER_TOKEN = 4
PI_OVERHEAD_TOKENS = 2_000  # pi's system prompt and tool schemas, not work Claude would do


def tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def charge(item_id: str, tool: str, cost: int) -> None:
    _append("tokens.jsonl", {"id": item_id, "tool": tool, "cost": cost, "at": time.time()})


def native(item_id: str, tool: str, estimate: int, basis: str) -> None:
    _append(
        "tokens.jsonl",
        {
            "id": item_id,
            "tool": tool,
            "native": max(0, estimate),
            "basis": basis,
            "at": time.time(),
        },
    )


def ledger() -> dict[str, dict[str, Any]]:
    """Per item: tool, cost, native estimate, outcome (if rated)."""
    items: dict[str, dict[str, Any]] = {}
    for r in _read("tokens.jsonl"):
        it = items.setdefault(r["id"], {"tool": r.get("tool", ""), "cost": 0, "native": 0})
        it["cost"] += int(r.get("cost") or 0)
        it["native"] += int(r.get("native") or 0)
        if r.get("tool") and it["tool"] in ("", "overhead"):
            it["tool"] = r["tool"]
    for r in _read("feedback.jsonl"):
        if r.get("id") in items:
            items[r["id"]]["outcome"] = r["outcome"]
    return items


def savings() -> dict[str, dict[str, int]]:
    """Per tool (plus "all"): cost, native, gross saved, net saved, items, rated items."""
    out: dict[str, dict[str, int]] = {}
    for it in ledger().values():
        tool = it["tool"] or "other"
        for key in (tool, "all"):
            s = out.setdefault(
                key, {"items": 0, "rated": 0, "cost": 0, "native": 0, "gross": 0, "net": 0}
            )
            gross = it["native"] - it["cost"]
            lost = it.get("outcome") in ("redone", "unused")
            s["items"] += 1
            s["rated"] += "outcome" in it
            s["cost"] += it["cost"]
            s["native"] += it["native"]
            s["gross"] += gross
            s["net"] += -it["cost"] if lost else gross
    return out


def savings_report() -> str:
    s = savings()
    if not s:
        return "No token use recorded yet."
    rows = [
        "| tool | items | rated | cost to Claude | native estimate | saved (gross) | saved (net) |",
        "|---|---|---|---|---|---|---|",
    ]
    for tool in sorted(s, key=lambda t: (t == "all", t)):
        v = s[tool]
        rows.append(
            f"| {tool} | {v['items']} | {v['rated']} | {v['cost']:,} | {v['native']:,} | "
            f"{v['gross']:,} | {v['net']:,} |"
        )
    rows += [
        "",
        "Tokens are estimates (about 4 characters each). Net counts results rated "
        "redone or unused as losses; rate results with the feedback tool.",
    ]
    return "\n".join(rows)


def track_record() -> dict[str, dict[str, Any]]:
    """Per tool: typical seconds (median of calls, or of finished job attempts) and how
    rated results came out."""
    secs: dict[str, list[float]] = defaultdict(list)
    for c in _read("calls.jsonl"):
        if c.get("seconds") is not None:
            secs[c.get("tool", "")].append(float(c["seconds"]))
    jroot = state_dir() / "jobs"
    if jroot.is_dir():
        for p in jroot.glob("*/job.json"):
            try:
                j = json.loads(p.read_text())
            except ValueError:
                continue
            if j.get("finished") and j.get("started") and j.get("state") == "done":
                tool = "research" if j.get("kind") == "research" else "delegate"
                secs[tool].append(float(j["finished"]) - float(j["started"]))
    rated: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in {r["id"]: r for r in _read("feedback.jsonl") if r.get("id")}.values():
        rated[r.get("tool", "")][r["outcome"]] += 1
    out: dict[str, dict[str, Any]] = {}
    for tool in set(secs) | set(rated):
        xs = sorted(secs.get(tool, []))
        out[tool] = {
            "median_s": xs[len(xs) // 2] if xs else None,
            "runs": len(xs),
            "outcomes": dict(rated.get(tool, {})),
        }
    return out
