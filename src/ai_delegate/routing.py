"""Picking a model: prefer what's loaded, then what fits, and never a reserved machine.

Swapping a model in takes from ten seconds to minutes and can push out a model ai-factory or
someone else is using, so a loaded candidate wins over a better-ranked one that would need a
load. Naming a model outright overrides the ranking, but not a reservation.

A model's record for the tool (its recent rated results, see outcomes.ratings) moves it
within the list: a good record ahead of models without one, a poor one to the end. ai-bench
measures models on benchmarks; the record measures them on this user's work.
"""

from __future__ import annotations

from collections.abc import Sequence
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
    # On a swap: models already loaded on that machine whose name contains this one's (a
    # fine-tune or quant of it, say). Reported, never substituted.
    variants: list[str] = field(default_factory=list)


def loaded_variants(cluster: Cluster, m: Model) -> list[str]:
    """Names of other loaded models on m's machine whose name contains m's base name."""
    base = m.name.split("@", 1)[0].lower()
    return sorted(
        {
            o.name
            for o in cluster.models
            if o.machine == m.machine and o.loaded and o.name != m.name and base in o.name.lower()
        }
    )


# The record: the most recent RECORD_RECENT rated results, counted once there are at least
# RECORD_MIN. Used (kept or fixed) in RECORD_GOOD of them moves a model up; under RECORD_POOR,
# to the end. In between, or without enough ratings, the list order stands.
RECORD_RECENT = 6
RECORD_MIN = 3
RECORD_GOOD = 0.75
RECORD_POOR = 0.5
USED = ("kept", "fixed")

Ratings = Sequence[tuple[str, str, str]]  # (model, machine, outcome), oldest first


def record_of(m: Model, ratings: Ratings) -> tuple[int, int]:
    """(used, rated) over m's most recent rated results."""
    mine = [o for name, machine, o in ratings if machine == m.machine and m.matches(name)]
    recent = mine[-RECORD_RECENT:]
    return sum(o in USED for o in recent), len(recent)


def record_tier(used: int, rated: int) -> int:
    """0: a good record, 1: none or middling, 2: a poor one."""
    if rated < RECORD_MIN:
        return 1
    share = used / rated
    return 0 if share >= RECORD_GOOD else 2 if share < RECORD_POOR else 1


def pick(
    cluster: Cluster,
    candidates: list[str],
    explicit: str | None = None,
    ratings: Ratings = (),
) -> Choice:
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

    # The record ranks first (not for a model named outright), then the list order; busy
    # copies sort after idle ones of the same rank.
    records = {m.id: record_of(m, ratings) for _, m in pool} if not explicit else {}
    tiers = {mid: record_tier(*r) for mid, r in records.items()}

    def key(rm: tuple[int, Model]) -> tuple[int, int, int]:
        return tiers.get(rm[1].id, 1), rm[0], rm[1].inflight

    def noted(m: Model, why: str) -> str:
        tier = tiers.get(m.id, 1)
        if tier == 1:
            return why
        used, rated = records[m.id]
        moved = "ranked up" if tier == 0 else "ranked down"
        return f"{why}; {moved} by its record ({used} of {rated} recent results used)"

    loaded = sorted((rm for rm in pool if rm[1].loaded), key=key)
    if loaded:
        m = loaded[0][1]
        why = "loaded" if loaded[0][0] == 0 else f"loaded (preferred over {wanted[0]}, not loaded)"
        return Choice(m, "loaded", noted(m, why), skipped)

    def fits(m: Model) -> bool:
        machine = cluster.machines.get(m.machine)
        return machine is not None and m.memory_gb > 0 and m.memory_gb <= machine.free_gb

    fitting = sorted((rm for rm in pool if fits(rm[1])), key=key)
    if fitting:
        m = fitting[0][1]
        why = f"not loaded; loads into free memory on {m.machine}"
        return Choice(m, "fits", noted(m, why), skipped)

    m = sorted(pool, key=key)[0][1]
    why = f"not loaded; loading it makes {m.machine} unload idle models"
    variants = loaded_variants(cluster, m)
    if variants:
        why += f"; loaded on {m.machine} instead: {', '.join(variants)}"
    return Choice(m, "swap", noted(m, why), skipped, variants)
