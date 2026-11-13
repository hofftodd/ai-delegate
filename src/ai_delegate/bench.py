"""Measured routing (M3): each tool's candidate list, generated from ai-bench results.

ai-bench keeps one results table per machine (``dashboard/data-mac/models.csv`` for the
Mac, ``dashboard/data/models.csv`` for the R9700 box). A score belongs to a model *on a
machine and engine*, so the generated lists name pinned copies (``flint@studio``).

Matching an ai-swap model to an ai-bench row, on that machine's table:

1. an explicit entry in ``bench.names`` (``"deepseek-v4-flash@studio": ds4-q4``);
2. the row named ``<engine>-<name>`` (ai-bench's name for e.g. flint on oMLX);
3. the row named exactly ``<name>``.

What each tool wants (the columns are ai-bench's):

- ``ask``: quick, correct answers. Quality is LiveCodeBench at 64k (else at 32k); models
  slower than ``ask_min_tps`` generating at 8k context are left out, since a quick answer
  that takes minutes isn't one.
- ``review``: the most careful reasoner, LiveCodeBench first, terminal-x3 to break ties,
  with a lower speed floor.
- ``research``: the delegate candidates (it is an agent loop too), reordered by LiveCodeBench,
  since the work is reading and reasoning rather than editing.
- ``delegate``: agentic coding, so terminal-x3 (multi-step terminal tasks) first, then
  terminal-quick, LiveCodeBench to break ties; ``delegate_min_tps`` floor, since a job
  makes many calls.

Models ai-bench hasn't measured on the right benchmark aren't ranked; the report says so.
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ai_delegate.swap import Cluster, Model

DEFAULT_TABLES = {
    "studio": "dashboard/data-mac/models.csv",
    "mini-ubuntu": "dashboard/data/models.csv",
}


# ai-swap engine -> the ai-bench backends that measured that engine. A row from another
# engine says little about this copy (llama.cpp vs DwarfStar for DeepSeek: 35 vs 56 tok/s).
ENGINE_BACKENDS = {
    "llamacpp": {"metal", "metal-b12192", "swap", "vulkan", "llamacpp", "llamacpp-rocm"},
    "omlx": {"omlx"},
    "ds4": {"ds4"},
    "sushi": {"sushi"},
    "strata": {"strata"},
}


@dataclass
class BenchConfig:
    path: str = "~/dev/ai-tools/ai-bench"
    tables: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TABLES))
    names: dict[str, str] = field(default_factory=dict)  # "model@machine" or "model" -> row
    ask_min_tps: float = 75.0
    review_min_tps: float = 20.0
    delegate_min_tps: float = 30.0
    delegate_min_pct: float = 75.0  # best of terminal-x3 / terminal-quick
    top: int = 5


def _num(row: dict[str, str], key: str) -> float | None:
    v = (row.get(key) or "").strip()
    try:
        return float(v) if v else None
    except ValueError:
        return None


@dataclass
class Scored:
    copy: str  # name@machine
    row: str  # the ai-bench model it matched
    lcb: float | None
    tx3: float | None
    tquick: float | None
    tps: float | None

    @property
    def quality(self) -> float | None:
        return self.lcb


def load_tables(cfg: BenchConfig) -> dict[str, dict[str, dict[str, str]]]:
    root = Path(cfg.path).expanduser()
    out: dict[str, dict[str, dict[str, str]]] = {}
    for machine, rel in cfg.tables.items():
        p = Path(rel).expanduser()
        p = p if p.is_absolute() else root / p
        if p.is_file():
            with p.open() as f:
                out[machine] = {r["model"]: r for r in csv.DictReader(f) if r.get("model")}
    return out


def match(
    m: Model, table: dict[str, dict[str, str]], names: dict[str, str]
) -> tuple[str | None, str]:
    """(row, why not) for one copy: the matching ai-bench row, or None and the reason."""
    for key in (f"{m.name}@{m.machine}", m.name):
        if key in names:
            row = names[key]
            return (row, "") if row in table else (None, f"bench.names says {row!r}, not found")
    if m.engine and f"{m.engine}-{m.name}" in table:
        return f"{m.engine}-{m.name}", ""
    if m.name in table:
        backend = table[m.name].get("backend", "")
        allowed = ENGINE_BACKENDS.get(m.engine)
        if allowed is None or backend in allowed:
            return m.name, ""
        return None, f"measured on {backend}, served by {m.engine}"
    return None, "not in the table"


def score(cluster: Cluster, cfg: BenchConfig) -> tuple[list[Scored], list[str]]:
    tables = load_tables(cfg)
    scored: list[Scored] = []
    unmatched: list[str] = []
    for m in cluster.models:
        table = tables.get(m.machine)
        if table is None:
            continue
        row_name, why = match(m, table, cfg.names)
        if row_name is None:
            unmatched.append(f"{m.name}@{m.machine} ({why})")
            continue
        row = table[row_name]
        lcb = _num(row, "lcb64k_pass_pct")
        if lcb is None:
            lcb = _num(row, "lcb_pass_pct")
        scored.append(
            Scored(
                copy=f"{m.name}@{m.machine}",
                row=row_name,
                lcb=lcb,
                tx3=_num(row, "terminal_x3_pass_pct"),
                tquick=_num(row, "terminal_quick_pass_pct"),
                tps=_num(row, "tg8k_tok_s"),
            )
        )
    return scored, unmatched


def _z(v: float | None) -> float:
    return v if v is not None else -1.0


def rank(scored: list[Scored], cfg: BenchConfig) -> dict[str, list[Scored]]:
    def fast(s: Scored, floor: float) -> bool:
        return s.tps is not None and s.tps >= floor

    ask = [s for s in scored if s.lcb is not None and fast(s, cfg.ask_min_tps)]
    ask.sort(key=lambda s: (_z(s.lcb), _z(s.tps)), reverse=True)
    review = [s for s in scored if s.lcb is not None and fast(s, cfg.review_min_tps)]
    review.sort(key=lambda s: (_z(s.lcb), _z(s.tx3), _z(s.tps)), reverse=True)
    delegate = [
        s
        for s in scored
        if max(_z(s.tx3), _z(s.tquick)) >= cfg.delegate_min_pct and fast(s, cfg.delegate_min_tps)
    ]
    delegate.sort(key=lambda s: (_z(s.tx3), _z(s.tquick), _z(s.lcb), _z(s.tps)), reverse=True)
    # Research is an agent loop too, but read-only: terminal skill to drive the tools, then
    # reasoning (LiveCodeBench) over what it reads, among the delegate candidates.
    research = sorted(delegate, key=lambda s: (_z(s.lcb), _z(s.tx3), _z(s.tps)), reverse=True)
    lists = (("ask", ask), ("review", review), ("delegate", delegate), ("research", research))
    return {k: v[: cfg.top] for k, v in lists}


def routes_doc(ranked: dict[str, list[Scored]], scored: list[Scored], source: str) -> dict:
    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": source,
        "routes": {tool: [s.copy for s in lst] for tool, lst in ranked.items()},
        "scores": {
            s.copy: {
                k: v
                for k, v in (
                    ("bench", s.row),
                    ("lcb", s.lcb),
                    ("terminal_x3", s.tx3),
                    ("terminal_quick", s.tquick),
                    ("tok_s", s.tps),
                )
                if v is not None
            }
            for s in scored
        },
    }


def write_routes(doc: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Generated by `ai-delegate routes --write` from ai-bench results; regenerate rather\n"
        "# than edit. Lists in config.yaml (ask.models, ...) override these.\n"
    )
    path.write_text(header + yaml.safe_dump(doc, sort_keys=False))


def report(ranked: dict[str, list[Scored]], unmatched: list[str]) -> str:
    def fmt(v: float | None, unit: str = "") -> str:
        return f"{v:g}{unit}" if v is not None else "-"

    lines = []
    for tool, lst in ranked.items():
        lines.append(f"{tool}:")
        if not lst:
            lines.append("  (no measured model qualifies)")
        for i, s in enumerate(lst, 1):
            lines.append(
                f"  {i}. {s.copy:44} lcb {fmt(s.lcb, '%'):6} tx3 {fmt(s.tx3, '%'):6} "
                f"tq {fmt(s.tquick, '%'):6} {fmt(s.tps)} tok/s  [{s.row}]"
            )
    if unmatched:
        lines.append(
            "unmatched (map them under bench.names in config.yaml if ai-bench calls them "
            "something else):\n" + "\n".join(f"  {u}" for u in sorted(unmatched))
        )
    return "\n".join(lines)
