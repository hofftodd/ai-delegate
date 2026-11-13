"""Picking a model: prefer what's loaded, then what fits, and never a reserved machine.

Swapping a model in takes from ten seconds to minutes and can push out a model ai-factory or
someone else is using, so a loaded candidate wins over a better-ranked one that would need a
load. Naming a model outright overrides the ranking, but not a reservation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_delegate.swap import Cluster, Model


class NoModel(Exception):
    """No candidate can be used; the message says why, for the caller."""


@dataclass
class Choice:
    model: Model
    load: str  # "loaded", "fits" (loads into free memory) or "swap" (pushes idle models out)
    why: str
    skipped: list[str] = field(default_factory=list)


def pick(cluster: Cluster, candidates: list[str], explicit: str | None = None) -> Choice:
    wanted = [explicit] if explicit else candidates
    skipped: list[str] = []
    pool: list[tuple[int, Model]] = []
    seen: set[str] = set()
    for rank, name in enumerate(wanted):
        found = cluster.find(name)
        if not found:
            skipped.append(f"{name}: not offered by ai-swap")
        for m in found:
            if m.id in seen:
                continue
            seen.add(m.id)
            machine = cluster.machines.get(m.machine)
            if machine and machine.reserved_by:
                skipped.append(f"{m.id}: {m.machine} is reserved by {machine.reserved_by}")
            elif not m.runnable:
                skipped.append(f"{m.id}: not runnable on {m.machine}")
            else:
                pool.append((rank, m))

    if not pool:
        if explicit and not cluster.find(explicit):
            names = ", ".join(sorted({m.id for m in cluster.models})) or "none"
            raise NoModel(f"no model called {explicit!r}. Available: {names}")
        raise NoModel("no usable model: " + "; ".join(skipped))

    # Busy copies sort after idle ones of the same rank; rank decides within each tier.
    def key(rm: tuple[int, Model]) -> tuple[int, int]:
        return rm[0], rm[1].inflight

    loaded = sorted((rm for rm in pool if rm[1].loaded), key=key)
    if loaded:
        m = loaded[0][1]
        why = "loaded" if loaded[0][0] == 0 else f"loaded (preferred over {wanted[0]}, not loaded)"
        return Choice(m, "loaded", why, skipped)

    def fits(m: Model) -> bool:
        machine = cluster.machines.get(m.machine)
        return machine is not None and m.memory_gb > 0 and m.memory_gb <= machine.free_gb

    fitting = sorted((rm for rm in pool if fits(rm[1])), key=key)
    if fitting:
        m = fitting[0][1]
        return Choice(m, "fits", f"not loaded; loads into free memory on {m.machine}", skipped)

    m = sorted(pool, key=key)[0][1]
    return Choice(
        m, "swap", f"not loaded; loading it makes {m.machine} unload idle models", skipped
    )
