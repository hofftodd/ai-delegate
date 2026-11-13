import csv
from pathlib import Path

from ai_delegate import bench, config
from ai_delegate.swap import Cluster, Machine, Model

COLS = [
    "model",
    "backend",
    "lcb64k_pass_pct",
    "lcb_pass_pct",
    "terminal_x3_pass_pct",
    "terminal_quick_pass_pct",
    "tg8k_tok_s",
]


def table(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        w.writerows(rows)


def setup(tmp_path: Path) -> tuple[Cluster, bench.BenchConfig]:
    table(tmp_path / "mac.csv", [
        ["omlx-fast", "omlx", "70", "", "90", "", "160"],
        ["fast", "metal", "60", "", "", "", "90"],  # same model on another engine
        ["deep", "metal", "80", "", "", "", "35"],  # served by ds4: wrong engine
        ["ds4-q4", "ds4", "93", "", "100", "", "56"],
    ])  # fmt: skip
    table(tmp_path / "linux.csv", [
        ["thinker", "swap", "80", "", "98", "", "53"],
        ["coder", "swap", "", "60", "100", "100", "52"],
        ["tiny", "swap", "", "", "", "50", "85"],
        ["slow", "swap", "90", "", "", "", "10"],
    ])  # fmt: skip
    models = [
        Model("fast", "fast", "mac", "ready", engine="omlx"),
        Model("deep", "deep", "mac", "ready", engine="ds4"),
        Model("thinker", "thinker", "linux", "ready", engine="llamacpp"),
        Model("coder", "coder", "linux", "stopped", engine="llamacpp"),
        Model("tiny", "tiny", "linux", "stopped", engine="llamacpp"),
        Model("slow", "slow", "linux", "stopped", engine="llamacpp"),
        Model("unknown", "unknown", "linux", "stopped", engine="llamacpp"),
    ]
    cl = Cluster({"mac": Machine("mac"), "linux": Machine("linux")}, models)
    cfg = bench.BenchConfig(path=str(tmp_path), tables={"mac": "mac.csv", "linux": "linux.csv"})
    return cl, cfg


def test_match_by_engine_and_mapping(tmp_path):
    cl, cfg = setup(tmp_path)
    scored, unmatched = bench.score(cl, cfg)
    by = {s.copy: s for s in scored}
    assert by["fast@mac"].row == "omlx-fast" and by["fast@mac"].tps == 160
    assert "deep@mac (measured on metal, served by ds4)" in unmatched
    assert any(u.startswith("unknown@linux (not in the table)") for u in unmatched)
    assert by["coder@linux"].lcb == 60  # falls back to the 32k LiveCodeBench column

    cfg.names = {"deep@mac": "ds4-q4"}
    scored, unmatched = bench.score(cl, cfg)
    assert {s.copy: s.row for s in scored}["deep@mac"] == "ds4-q4"
    assert not any(u.startswith("deep@mac") for u in unmatched)


def test_rank_per_tool(tmp_path):
    cl, cfg = setup(tmp_path)
    cfg.names = {"deep@mac": "ds4-q4"}
    ranked = bench.rank(bench.score(cl, cfg)[0], cfg)
    copies = {tool: [s.copy for s in lst] for tool, lst in ranked.items()}
    assert copies["ask"] == ["fast@mac"]  # the only one over the 75 tok/s floor with an LCB
    assert copies["review"][:2] == ["deep@mac", "thinker@linux"]
    assert "slow@linux" not in copies["review"]  # 10 tok/s: under the review floor
    # Both 100% on terminal-x3; coder also has terminal-quick, deep was never run on it.
    assert copies["delegate"][:2] == ["coder@linux", "deep@mac"]
    assert "tiny@linux" not in copies["delegate"]  # 50%: under delegate_min_pct


def test_routes_file_layers_under_config(tmp_path):
    cl, cfg_b = setup(tmp_path)
    ranked = bench.rank(bench.score(cl, cfg_b)[0], cfg_b)
    scored = bench.score(cl, cfg_b)[0]
    bench.write_routes(bench.routes_doc(ranked, scored, "x"), tmp_path / "routes.yaml")
    (tmp_path / "config.yaml").write_text("review:\n  models: [mine]\n")
    cfg = config.load(tmp_path / "config.yaml")
    assert cfg.routes["ask"] == ["fast@mac"]
    assert cfg.route_source["ask"].startswith("ai-bench (")
    assert cfg.routes["review"] == ["mine"] and cfg.route_source["review"] == "config.yaml"
    assert cfg.scores["fast@mac"]["tok_s"] == 160
