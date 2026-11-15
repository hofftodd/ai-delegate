"""Where a job's agent and tests run: a Docker container (the default) or a local process.

Docker, as in ai-factory: one container per step with only the job's clone mounted
(read-write, at /workspace) and its control dir (read-only, at /ai-delegate). Containers sit
on an internal Docker network with no route anywhere; the only other thing on it is a relay
container that forwards one port to this process's gateway. So the agent reaches its model
through the gateway's budgets, and nothing else: not the LAN, ai-swap directly, or the
internet. Each ai-delegate process has its own gateway port, so its own relay
(``ai-delegate-gw-<port>``, reached as ``gw-<port>`` on the network).

Host: the same command as a host process under srt (Anthropic's sandbox-runtime:
bubblewrap on Linux, Seatbelt on macOS), so it keeps the host's toolchains and caches.
Writes are limited to the clone and the job's scratch dir; credential stores and other
jobs can't be read; the network is srt's proxy, which allows only the gateway's
``127.0.0.1:<port>``. Weaker than Docker (same kernel, same uid, the host's files readable
by default), much stronger than local.

Local: the same command as a host process group in the clone, with no isolation at all. For
machines without Docker or srt; results say so.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ai_common import sandbox as aisb

WORKSPACE = "/workspace"
CTL = "/ai-delegate"
NETWORK = "ai-delegate-exec"
RELAY_PORT = 8701
HOST_ALIAS = "host.docker.internal"
LABEL = "ai-delegate=1"


# The Docker plumbing is shared with ai-factory through ai-common.
SandboxError = aisb.SandboxError
docker = aisb.docker
docker_ok = aisb.docker_ok
image_present = aisb.image_present


TOOLCHAIN_FILES = (".ai-delegate/Dockerfile", ".ai-factory/Dockerfile")


def toolchain_dockerfile(repo: Path) -> Path | None:
    """A project's own sandbox image: a Dockerfile that adds its toolchain on top of the
    base image (ai-factory's convention, which ai-delegate shares)."""
    for rel in TOOLCHAIN_FILES:
        if (repo / rel).is_file():
            return repo / rel
    return None


def resolve_image(repo: Path, default: str, project: str) -> tuple[str, str]:
    """(image, note): the project's toolchain image, built and cached by its Dockerfile's
    hash, or the default image."""
    dockerfile = toolchain_dockerfile(repo)
    if dockerfile is None:
        return default, f"{default} (no toolchain: add .ai-delegate/Dockerfile for one)"
    digest = hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:12]
    name = "".join(c if c.isalnum() or c in "-_." else "-" for c in project.lower())
    tag = f"ai-delegate/{name}:{digest}"
    if not image_present(tag):
        r = docker(
            "build", "-t", tag, "-f", str(dockerfile), "--label", LABEL, str(dockerfile.parent),
            timeout=1800,
        )  # fmt: skip
        if r.returncode != 0:
            raise SandboxError(f"building {dockerfile} failed: {r.stderr.strip()[-600:]}")
    return tag, f"{tag} (from {dockerfile.relative_to(repo)})"


def gateway_bind_host() -> str:
    # Docker Desktop and OrbStack forward host.docker.internal to the Mac's loopback. On
    # Linux the containers come in over the bridge, so the gateway must listen beyond it;
    # its URLs carry a per-job secret, as ai-factory's do.
    return "127.0.0.1" if sys.platform == "darwin" else "0.0.0.0"


def relay_alias(gateway_port: int) -> str:
    return f"gw-{gateway_port}"


def ensure_relay(image: str, gateway_port: int) -> str:
    """Make sure the internal network and this process's relay exist; returns the base
    URL containers use to reach the gateway."""
    relay = aisb.Relay(
        name=f"ai-delegate-gw-{gateway_port}",
        alias=relay_alias(gateway_port),
        network=NETWORK,
        image=image,
        labels={
            "ai-delegate": "1",
            "ai-delegate.role": "relay",
            "ai-delegate.pid": str(os.getpid()),
        },
    )
    return aisb.ensure_relay(relay, gateway_port)


def remove_relay(gateway_port: int) -> None:
    docker("rm", "-f", f"ai-delegate-gw-{gateway_port}")


def sweep_stale() -> None:
    """Remove relays and job containers left by ai-delegate processes that have exited."""
    r = docker("ps", "-a", "--filter", "label=ai-delegate", "--format",
               '{{.Names}} {{.Label "ai-delegate.pid"}}')  # fmt: skip
    for row in r.stdout.splitlines():
        name, _, pid = row.partition(" ")
        if pid.isdigit() and not _alive(int(pid)):
            docker("rm", "-f", name)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Step:
    name: str
    workspace: Path
    ctl: Path
    command: list[str]
    env: dict[str, str]


Proc = aisb.Proc


async def start_docker(step: Step, image: str, cpus: str, memory: str) -> Proc:
    spec = aisb.RunSpec(
        name=step.name,
        workspace=step.workspace,
        ctl_dir=step.ctl,
        ctl_mount=CTL,
        image=image,
        command=step.command,
        env={"AI_DELEGATE_CTL": CTL, **step.env},
        labels={"ai-delegate": "1", "ai-delegate.pid": str(os.getpid())},
        cpus=cpus,
        memory=memory,
        network=NETWORK,
    )
    return await aisb.start(spec, max_line_bytes=None, cut_label="ai-delegate")


async def start_local(step: Step) -> Proc:
    env = {**os.environ, "AI_DELEGATE_CTL": str(step.ctl), **step.env}
    proc = await asyncio.create_subprocess_exec(
        *step.command,
        cwd=step.workspace,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    return Proc(proc, container=None, max_line_bytes=None)


# -- host: srt ------------------------------------------------------------------------------

# Never readable from a host-sandboxed job, whatever the repo: credentials, and the state of
# ai-delegate itself (other jobs' clones; the job's own dir is let back in).
HOST_DENY_READ = (
    "~/.ssh", "~/.gnupg", "~/.aws", "~/.azure", "~/.kube", "~/.docker", "~/.netrc",
    "~/.npmrc", "~/.pypirc", "~/.git-credentials", "~/.password-store",
    "~/.config/gh", "~/.config/gcloud", "~/.config/ai-delegate", "~/.local/state/ai-delegate",
    "~/.claude", "~/.claude.json", "~/Library/Keychains",
)  # fmt: skip
# The only variables a host job inherits: no tokens or keys from the parent's environment.
HOST_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TZ")
HOST_TOOLS_LINUX = ("bwrap", "socat", "rg")  # srt's own dependencies on Linux


def _expand(path: str) -> str:
    return str(Path(path).expanduser())


def host_settings(
    job_dir: Path, workspace: Path, gateway_port: int, allow_write: list[str], deny_read: list[str]
) -> dict[str, object]:
    """srt's settings for one job: write the clone and the job's scratch dir, read anything
    but HOST_DENY_READ, reach only the gateway."""
    return {
        "network": {"allowedDomains": [f"127.0.0.1:{gateway_port}"], "deniedDomains": []},
        "filesystem": {
            "denyRead": [_expand(p) for p in (*HOST_DENY_READ, *deny_read)],
            "allowRead": [str(job_dir)],
            "allowWrite": [str(workspace), str(job_dir / "tmp"), *map(_expand, allow_write)],
            "denyWrite": [],
        },
    }


def host_ok(srt: str) -> str:
    """'' if srt can run a command here, else why not (missing tools, or a kernel that
    refuses unprivileged user namespaces, as Ubuntu 24.04 does by default)."""
    if not shutil.which(srt):
        return f"{srt} is not installed (npm i -g @anthropic-ai/sandbox-runtime)"
    if sys.platform.startswith("linux"):
        missing = [t for t in HOST_TOOLS_LINUX if not shutil.which(t)]
        if missing:
            return f"srt needs {', '.join(missing)} (apt install bubblewrap socat ripgrep)"
    with tempfile.TemporaryDirectory(prefix="ai-delegate-srt-") as d:
        settings = Path(d) / "srt.json"
        empty: dict[str, dict[str, list[str]]] = {
            "network": {"allowedDomains": [], "deniedDomains": []},
            "filesystem": {"denyRead": [], "allowWrite": [], "denyWrite": []},
        }  # srt refuses a file missing any of these keys
        settings.write_text(json.dumps(empty))
        try:
            r = subprocess.run(
                [srt, "-s", str(settings), "-c", "true"],
                capture_output=True, text=True, timeout=60, check=False,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            return "srt timed out running `true`"
    if r.returncode != 0:
        why = (r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}")[-400:]
        hint = (
            " (on Ubuntu 24.04+: sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0)"
            if "ermission" in why or "uid map" in why
            else ""
        )
        return f"srt can't run a sandboxed command: {why}{hint}"
    return ""


def host_command(srt: str, settings: Path, scratch: Path, command: list[str]) -> list[str]:
    """``command`` under srt. srt sets NO_PROXY to cover 127.0.0.1, which would send the
    gateway call around the proxy into a refused connection, so it is unset inside; Node
    (pi) needs NODE_USE_ENV_PROXY to use the proxy at all. TMPDIR is set inside too: srt
    puts its own sockets in TMPDIR, and the job's path is too long for one on macOS."""
    inner = (
        f"unset NO_PROXY no_proxy; export NODE_USE_ENV_PROXY=1 TMPDIR={shlex.quote(str(scratch))}"
        "; exec " + shlex.join(command)
    )
    return [srt, "-s", str(settings), "-c", inner]


async def start_host(step: Step, srt: str, settings: Path, scratch: Path) -> Proc:
    scratch.mkdir(exist_ok=True)
    env = {k: os.environ[k] for k in HOST_ENV if k in os.environ}
    env |= {"AI_DELEGATE_CTL": str(step.ctl), **step.env}
    proc = await asyncio.create_subprocess_exec(
        *host_command(srt, settings, scratch, step.command),
        cwd=step.workspace,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    return Proc(proc, container=None, max_line_bytes=None)
