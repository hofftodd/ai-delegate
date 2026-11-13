import subprocess
from pathlib import Path

from ai_delegate import citations

CODE = '''"""Module docstring that mentions make_relay and connect_network in passing."""

import os


def helper():
    return 1


def make_relay(port):
    name = f"relay-{port}"
    connect_network(name, alias=port)
    return name
'''


def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    (r / "src" / "pkg").mkdir(parents=True)
    (r / "src" / "pkg" / "relay.py").write_text(CODE)
    (r / "src" / "pkg" / "other.py").write_text("x = 1\n")
    (r / "tests").mkdir()
    (r / "tests" / "other.py").write_text("y = 2\n")
    subprocess.run(["git", "-C", str(r), "init", "-q"], check=True)
    return r


def test_verified_moved_bad_and_unanchored(tmp_path):
    r = repo(tmp_path)
    report = "\n".join([
        "- `make_relay` builds the name `relay-{port}` (src/pkg/relay.py:10-11).",
        "- It calls `connect_network(name, alias=port)` (`relay.py:3`).",  # suffix path, wrong line
        "- Nothing here (src/pkg/missing.py:4) or there (src/pkg/relay.py:99).",
        "- Ambiguous `x` lives in other.py:1.",
        "- The helper exists (src/pkg/relay.py:6).",  # no code quoted
    ])  # fmt: skip
    res = citations.check(report, r)
    assert res.total == 6
    assert res.verified == 1
    assert res.moved == ["relay.py:3: the quoted code is probably at src/pkg/relay.py:12"]
    assert "src/pkg/missing.py:4 (no such file)" in res.bad
    assert "src/pkg/relay.py:99 (src/pkg/relay.py has 13 lines)" in res.bad
    assert "other.py:1 (ambiguous: 2 files)" in res.bad
    assert res.unanchored == 1


def test_docstring_mentions_are_not_relocation_targets(tmp_path):
    r = repo(tmp_path)
    res = citations.check("`make_relay` and `connect_network` are at src/pkg/relay.py:6", r)
    assert res.moved == ["src/pkg/relay.py:6: the quoted code is probably at src/pkg/relay.py:10"]


def test_times_and_urls_are_not_citations(tmp_path):
    r = repo(tmp_path)
    assert citations.check("At 10:30 we hit http://studio.local:8080/v1.", r).total == 0
