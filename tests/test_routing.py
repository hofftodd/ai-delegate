import pytest
from conftest import models, status

from ai_delegate.routing import NoModel, pick
from ai_delegate.swap import parse_cluster


def test_parse_ids_and_copies(cluster):
    ids = {(m.id, m.machine) for m in cluster.models}
    assert ("flint", "studio") in ids and ("flint@mini", "mini") in ids
    assert [m.id for m in cluster.find("flint")] == ["flint", "flint@mini"]
    assert [m.id for m in cluster.find("flint@mini")] == ["flint@mini"]
    assert [m.id for m in cluster.find("S")] == ["small"]
    assert cluster.machines["studio"].free_gb == 40


def test_first_choice_loaded(cluster):
    c = pick(cluster, ["flint", "thinker"])
    assert (c.model.id, c.load, c.why) == ("flint", "loaded", "loaded")


def test_loaded_beats_better_ranked(cluster):
    c = pick(cluster, ["big", "thinker"])
    assert c.model.id == "thinker" and c.load == "loaded"
    assert "preferred over big" in c.why


def test_fits_before_swap(cluster):
    c = pick(cluster, ["big", "small"])
    assert (c.model.id, c.load) == ("small", "fits")


def test_swap_as_last_resort(cluster):
    c = pick(cluster, ["big"])
    assert (c.model.id, c.load) == ("big", "swap")


def test_explicit_prefers_loaded_copy(cluster):
    c = pick(cluster, ["thinker"], explicit="flint")
    assert c.model.id == "flint" and c.model.machine == "studio"


def test_reserved_machine_skipped():
    cl = parse_cluster(status(lock={"owner": "ai-bench", "reason": "speed run"}), models())
    c = pick(cl, ["thinker", "small"])
    assert c.model.id == "small"
    assert any("reserved by ai-bench (speed run)" in s for s in c.skipped)
    with pytest.raises(NoModel, match="reserved by ai-bench"):
        pick(cl, ["thinker"], explicit="thinker")


def test_offline_peer_models_absent():
    cl = parse_cluster(status(peer_online=False), models(peer_online=False))
    assert not cl.find("thinker")
    assert not cl.machines["mini"].online


def test_unknown_explicit_lists_models(cluster):
    with pytest.raises(NoModel, match="no model called 'nope'. Available: big, flint"):
        pick(cluster, [], explicit="nope")
