"""ai-delegate: an MCP server that hands bounded work to the local models behind ai-swap."""

from __future__ import annotations

import argparse
import asyncio
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ai-delegate", description=__doc__)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server over stdio (what Claude Code starts)")
    sub.add_parser("models", help="print the models table the models tool returns")
    sub.add_parser("doctor", help="check the config, ai-swap, Docker and the sandbox image")
    sub.add_parser("stats", help="what happened to delegated work: kept, fixed, redone")
    ro = sub.add_parser("routes", help="rank models per tool from ai-bench results")
    ro.add_argument("--write", action="store_true", help="save them to routes.yaml")
    sk = sub.add_parser("skill", help="link the Claude Code skill into ~/.claude/skills")
    sk.add_argument("--dest", default="~/.claude/skills", help="the skills directory")
    im = sub.add_parser("image", help="build the sandbox image from ai-factory's Dockerfile")
    im.add_argument("--from", dest="src", help="the executor-base directory to build")
    a = sub.add_parser("ask", help="ask a model from the terminal, as the ask tool would")
    a.add_argument("prompt")
    a.add_argument("-m", "--model")
    a.add_argument("-f", "--file", action="append", default=[])
    args = p.parse_args(argv)

    if args.cmd == "image":
        return _image(args.src)
    if args.cmd == "stats":
        from ai_delegate import outcomes

        print(outcomes.stats())
        print()
        print(outcomes.savings_report())
        return 0
    if args.cmd == "skill":
        return _skill(args.dest)

    from ai_delegate import server

    if args.cmd in (None, "serve"):
        server.serve()
        return 0
    if args.cmd == "routes":
        return asyncio.run(_routes(server, args.write))
    if args.cmd == "models":
        print(asyncio.run(server.models()))
        return 0
    if args.cmd == "ask":
        ctx = _TermContext()
        print(asyncio.run(server.ask(args.prompt, ctx, args.file, args.model)))  # type: ignore[arg-type]
        return 0
    return asyncio.run(_doctor(server))


class _TermContext:
    """Stands in for the MCP context outside a session: progress goes to stderr."""

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        print(f"  … {message}", file=sys.stderr)


async def _doctor(server) -> int:  # type: ignore[no-untyped-def]
    from ai_delegate.routing import NoModel, pick
    from ai_delegate.swap import SwapError

    cfg = server.cfg
    print(f"config:   {cfg.source}")
    print(f"endpoint: {cfg.endpoint}")
    try:
        cluster = await server.swap.cluster()
    except SwapError as e:
        print(f"FAIL      {e}")
        return 1
    loaded = sum(m.loaded for m in cluster.models)
    print(
        f"ai-swap:  ok, {len(cluster.models)} models on {len(cluster.machines)} machines, "
        f"{loaded} loaded"
    )
    ok = True
    for tool, names in cfg.routes.items():
        try:
            c = pick(cluster, names)
            print(
                f"{tool + ':':9} {c.model.id} on {c.model.machine} ({c.why}); list from "
                f"{cfg.route_source.get(tool, 'defaults')}"
            )
        except NoModel as e:
            ok = False
            print(f"{tool + ':':9} FAIL {e}")
        missing = [n for n in names if not cluster.find(n)]
        if missing:
            print(f"          not offered by ai-swap: {', '.join(missing)}")
    from ai_delegate import sandbox

    image = cfg.jobs.image
    if why := sandbox.docker_ok():
        print(f"docker:   not usable ({why}); delegate needs sandbox='local'")
    elif sandbox.image_present(image):
        print(f"docker:   ok, image {image} present")
    else:
        print(f"docker:   ok, but image {image} is missing: run `ai-delegate image`")
    print(
        f"jobs:     default sandbox {cfg.jobs.sandbox}, limit {cfg.jobs.time_limit_s / 60:.0f} "
        f"min, state in {_state_dir()}"
    )
    return 0 if ok else 1


async def _routes(server, write: bool) -> int:  # type: ignore[no-untyped-def]
    from ai_delegate import bench, config

    cfg = server.cfg
    cluster = await server.swap.cluster()
    scored, unmatched = bench.score(cluster, cfg.bench)
    if not scored:
        print(f"no ai-bench results matched under {cfg.bench.path}", file=sys.stderr)
        return 1
    ranked = bench.rank(scored, cfg.bench)
    print(bench.report(ranked, unmatched))
    overridden = [t for t, src in cfg.route_source.items() if src == "config.yaml"]
    if overridden:
        print(f"note: config.yaml sets its own list for {', '.join(overridden)}, which wins")
    if write:
        path = config.CONFIG_PATH.with_name("routes.yaml")
        bench.write_routes(bench.routes_doc(ranked, scored, cfg.bench.path), path)
        print(f"wrote {path}")
    return 0


def _state_dir() -> str:
    from ai_delegate.jobs import STATE_DIR

    return str(STATE_DIR)


def _skill(dest: str) -> int:
    """Symlink skills/ai-delegate into Claude Code's skills directory, so it updates with
    the checkout."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "skills" / "ai-delegate"
    if not (src / "SKILL.md").is_file():
        print(f"no skill at {src} (is this a source checkout?)", file=sys.stderr)
        return 1
    target = Path(dest).expanduser() / "ai-delegate"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() and target.resolve() == src:
        print(f"already linked: {target} -> {src}")
        return 0
    if target.exists() or target.is_symlink():
        print(f"{target} exists and isn't this skill; remove it first", file=sys.stderr)
        return 1
    target.symlink_to(src)
    print(f"linked {target} -> {src}")
    return 0


def _image(src: str | None) -> int:
    """Build ai-factory's executor image, which ai-delegate's sandbox shares."""
    import subprocess
    from pathlib import Path

    from ai_delegate import config

    here = Path(__file__).resolve()
    candidates = (
        [Path(src)]
        if src
        else [p / "ai-factory" / "docker" / "executor-base" for p in here.parents]
    )
    found = next((c for c in candidates if (c / "Dockerfile").is_file()), None)
    if found is None:
        print("can't find ai-factory/docker/executor-base; pass --from <dir>", file=sys.stderr)
        return 1
    tag = config.load().jobs.image
    print(f"building {tag} from {found}")
    return subprocess.run(["docker", "build", "-t", tag, str(found)], check=False).returncode


def cli() -> None:
    sys.exit(main())
