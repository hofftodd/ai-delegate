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


def test_swap_names_loaded_variants_without_substituting():
    st, ms = status(), models()
    st["node"]["models"].append({"name": "thinkingcap-big", "state": "ready", "memory_gb": 20})
    st["peers"][0]["node"]["models"].append({"name": "big-q4", "state": "ready", "memory_gb": 1})
    ms["data"] += [{"id": "thinkingcap-big", "node": "studio"}, {"id": "big-q4", "node": "mini"}]
    cl = parse_cluster(st, ms)
    c = pick(cl, [], explicit="big@studio")
    assert (c.model.id, c.model.machine, c.load) == ("big", "studio", "swap")
    assert c.variants == ["thinkingcap-big"]  # big-q4 is on another machine
    assert c.why.endswith("; loaded on studio instead: thinkingcap-big")


def test_swap_without_variants_says_nothing_extra(cluster):
    c = pick(cluster, ["big"])
    assert c.load == "swap" and c.variants == [] and "instead" not in c.why


def test_a_good_record_ranks_a_model_up(cluster):
    ratings = [("thinker", "mini", "kept")] * 3 + [("thinker", "mini", "fixed")]
    c = pick(cluster, ["flint", "thinker"], ratings=ratings)
    assert c.model.id == "thinker"
    assert "ranked up by its record (4 of 4 recent results used)" in c.why


def test_a_poor_record_ranks_a_model_down(cluster):
    ratings = [("flint", "studio", "unused")] * 3
    c = pick(cluster, ["flint", "thinker"], ratings=ratings)
    assert c.model.id == "thinker" and c.why.startswith("loaded")


def test_record_needs_enough_recent_ratings(cluster):
    # Two ratings are too few; old failures age out of the recent window.
    assert (
        pick(cluster, ["flint", "thinker"], ratings=[("flint", "studio", "unused")] * 2).model.id
        == "flint"
    )
    recovered = [("flint", "studio", "unused")] * 4 + [("flint", "studio", "kept")] * 6
    assert pick(cluster, ["thinker", "flint"], ratings=recovered).model.id == "flint"


def test_record_is_per_machine_and_never_beats_loaded(cluster):
    # flint@mini's record says nothing about the studio copy.
    ratings = [("flint", "mini", "unused")] * 3
    assert pick(cluster, ["flint", "thinker"], ratings=ratings).model.id == "flint"
    # A great record on a model that would need a load still loses to a loaded one.
    ratings = [("big", "studio", "kept")] * 5
    assert pick(cluster, ["thinker", "big"], ratings=ratings).model.id == "thinker"


def test_named_model_ignores_the_record(cluster):
    ratings = [("flint", "studio", "unused")] * 5
    assert pick(cluster, ["thinker"], explicit="flint", ratings=ratings).model.id == "flint"
