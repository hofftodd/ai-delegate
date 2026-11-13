"""Checking a research report's ``path:line`` citations against the repository.

Local models cite well enough to find the right file but often miss the line: pi's ``read``
tool shows file contents without line numbers, so a model counts by eye. Each citation is
checked in two steps:

1. **Does it resolve?** The file exists (as given, or as the one repo file whose path ends
   with it, since models often cite relative to the directory they were told to search) and
   the lines are within it.
2. **Is the cited code there?** The backticked code in the claim (between the previous
   citation on that line and this one) is looked for at the cited lines, give or take a few.
   Found: *verified*. Found on a code line elsewhere in that file (at least two distinct
   identifiers, weighted by how rare they are in the file): *moved*, with the line it's
   really on. Not quoted, or not found: *unanchored* (the file and lines exist; nothing more
   is known).

A verified citation still isn't proof the code means what the report says; it says the
report is pointing at the code it talks about.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# path:line or path:start-end; the path has a dot-extension or a slash, to skip times
# ("10:30") and URLs' ports.
CITE = re.compile(
    r"(?<![\w/.:-])((?:[\w.@+-]+/)*[\w.@+-]+\.\w+|(?:[\w.@+-]+/)+[\w.@+-]+):(\d+)(?:-(\d+))?"
)
SPAN = re.compile(r"`([^`\n]+)`")
TOKEN = re.compile(r"[A-Za-z_][\w-]*")
SLACK = 3  # lines either side of the cited range that still count as "there"
STOP = {
    "the", "and", "for", "with", "from", "that", "this", "not", "are", "its", "into", "self",
    "return", "none", "true", "false", "def", "class", "import", "async", "await",
}  # fmt: skip
COMMENT_STARTS = ("#", '"""', "'" * 3, "//", "/*", "* ", "- ")


@dataclass
class Result:
    total: int = 0
    verified: int = 0
    unanchored: int = 0
    moved: list[str] = field(default_factory=list)  # "cite: ... is probably at path:line"
    bad: list[str] = field(default_factory=list)  # missing file, ambiguous, out of range

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "verified": self.verified,
            "unanchored": self.unanchored,
            "moved": self.moved,
            "bad": self.bad,
        }


def _files(root: Path) -> list[str]:
    """Every file in the repo, relative to it (git's list: tracked and untracked, not ignored)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in out.splitlines() if line]


def resolve(path: str, root: Path, files: list[str]) -> str | None:
    """The repo-relative file a cited path means: as given, or the one file whose path ends
    with it."""
    rel = path.removeprefix("./")
    f = (root / rel).resolve()
    if f.is_relative_to(root) and f.is_file():
        return rel
    hits = [x for x in files if x == rel or x.endswith("/" + rel)]
    return hits[0] if len(hits) == 1 else None


def _tokens(span: str) -> list[str]:
    """The identifiers in a code span worth looking for (short and generic ones dropped)."""
    keep = (t for t in TOKEN.findall(span) if len(t) >= 3 and t.lower() not in STOP)
    return list(dict.fromkeys(keep))


def _anchors(line: str, start: int, prev_end: int) -> list[list[str]]:
    """Token lists of the code spans belonging to the citation that starts at line[start]:
    those between the previous citation on the line and this one. Spans are found on the
    whole line, so a citation's own backticks never flip the pairing."""
    out = []
    for m in SPAN.finditer(line):
        if not (prev_end <= m.start() < start):
            continue
        code = CITE.sub("", m.group(1))  # a span holding only a citation is no anchor
        toks = _tokens(code)
        if toks:
            out.append(toks)
    return out


def _words(text: str) -> set[str]:
    return set(TOKEN.findall(text))


def _code_lines(lines: list[str]) -> list[bool]:
    """Which lines are code: not blank, not a comment, not inside a docstring or other
    triple-quoted block."""
    out, inside = [], False
    quotes = ('"' * 3, "'" * 3)
    for text in lines:
        t = text.strip()
        n = sum(t.count(q) for q in quotes)
        if inside or n:
            out.append(False)
            if n % 2:
                inside = not inside
            continue
        out.append(bool(t) and not t.startswith(COMMENT_STARTS))
    return out


def _locate(anchors: list[list[str]], lines: list[str], lo: int, hi: int) -> int | None:
    """0 if an anchor's identifiers are at lines lo..hi (1-based, give or take SLACK); else
    the 1-based code line where the best anchor's are; None if nowhere convincing."""
    window = _words("\n".join(lines[max(0, lo - 1 - SLACK) : hi + SLACK]))
    for a in anchors:
        if sum(t in window for t in a) / len(a) >= 0.6:
            return 0
    per_line = [_words(x) for x in lines]
    freq: dict[str, int] = {}
    for ws in per_line:
        for w in ws:
            freq[w] = freq.get(w, 0) + 1
    code = _code_lines(lines)
    # Separately quoted names ("`make_relay` calls `connect_network`") place code together.
    union = list(dict.fromkeys(t for a in anchors for t in a))
    candidates = [*anchors, union] if len(anchors) > 1 else anchors
    best: tuple[float, float] = (0.0, 0.0)
    best_line = None
    for a in candidates:
        if len(a) < 2:
            continue  # one identifier can't place code
        weight = {t: 1.0 / freq.get(t, 1) for t in a}
        total = sum(weight.values())
        for i, ws in enumerate(per_line):
            if not code[i]:
                continue
            near = ws.union(*per_line[i + 1 : i + 1 + SLACK])
            found = [t for t in a if t in near]
            if len(found) < 2:
                continue
            score = sum(weight[t] for t in found) / total
            on_line = sum(weight[t] for t in a if t in ws) / total  # prefer the line itself
            if score >= 0.6 and (score, on_line) > best:
                best, best_line = (score, on_line), i + 1
    return best_line


def check(report: str, repo: Path) -> Result:
    res = Result()
    root = repo.resolve()
    files: list[str] | None = None
    contents: dict[str, list[str]] = {}
    seen: set[str] = set()
    for line in report.splitlines():
        prev_end = 0
        for m in CITE.finditer(line):
            path, start, end_s = m.group(1), int(m.group(2)), m.group(3)
            cite = m.group(0)
            anchors = _anchors(line, m.start(), prev_end)
            prev_end = m.end()
            if cite in seen:
                continue
            seen.add(cite)
            res.total += 1
            rel = resolve(path, root, [])
            if rel is None:
                files = _files(root) if files is None else files
                rel = resolve(path, root, files)
            if rel is None:
                n = sum(1 for x in files or [] if x.endswith("/" + path))
                res.bad.append(f"{cite} ({f'ambiguous: {n} files' if n else 'no such file'})")
                continue
            if rel not in contents:
                contents[rel] = (root / rel).read_text(errors="replace").splitlines()
            lines = contents[rel]
            last = int(end_s) if end_s else start
            if start < 1 or last > len(lines) or last < start:
                res.bad.append(f"{cite} ({rel} has {len(lines)} lines)")
                continue
            where = _locate(anchors, lines, start, last) if anchors else None
            if where == 0:
                res.verified += 1
            elif where is None:
                res.unanchored += 1
            else:
                res.moved.append(f"{cite}: the quoted code is probably at {rel}:{where}")
    return res
