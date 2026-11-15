import json

import pytest

from ai_delegate import outcomes


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_DELEGATE_STATE", str(tmp_path))
    return tmp_path


def test_feedback_needs_a_known_id_and_outcome(state):
    outcomes.log_call("c-aaaaaa", "ask", "flint", "studio", seconds=2.0)
    with pytest.raises(ValueError, match="outcome must be one of"):
        outcomes.record("c-aaaaaa", "great")
    with pytest.raises(ValueError, match="no call or job"):
        outcomes.record("c-nope", "kept")
    rec = outcomes.record("c-aaaaaa", "kept", "fine")
    assert (rec["tool"], rec["model"], rec["machine"]) == ("ask", "flint", "studio")


def test_feedback_on_a_job(state):
    (state / "jobs" / "j1").mkdir(parents=True)
    (state / "jobs" / "j1" / "job.json").write_text(
        json.dumps(
            {"id": "j1", "kind": "research", "model": "deepseek", "machine": "studio", "attempt": 2}
        )
    )
    rec = outcomes.record("j1", "fixed")
    assert rec["tool"] == "research" and rec["attempts"] == 2


def test_savings_gross_and_net(state):
    outcomes.charge("session-1", "overhead", 3000)
    for cid, tool, native, cost, outcome in [
        ("c-000001", "ask", 5000, 200, "kept"),
        ("c-000002", "review", 4000, 300, "redone"),  # a loss: only its cost counts
        ("c-000003", "ask", 1000, 100, None),  # unrated: face value
    ]:
        outcomes.log_call(cid, tool, "flint", "studio")
        outcomes.charge(cid, tool, cost)
        outcomes.native(cid, tool, native, "files")
        if outcome:
            outcomes.record(cid, outcome)
    s = outcomes.savings()
    assert s["ask"]["gross"] == 5700 and s["ask"]["net"] == 5700
    assert s["review"]["gross"] == 3700 and s["review"]["net"] == -300
    assert s["overhead"]["net"] == -3000
    assert s["all"]["net"] == 5700 - 300 - 3000 and s["all"]["rated"] == 2
    assert "| all | 4 | 2 |" in outcomes.savings_report()
    rec = outcomes.track_record()
    assert rec["review"]["outcomes"] == {"redone": 1}


def _job(state_dir, jid, **fields):
    (state_dir / "jobs" / jid).mkdir(parents=True)
    (state_dir / "jobs" / jid / "job.json").write_text(json.dumps({"id": jid, **fields}))


def test_track_record_counts_every_ended_attempt_by_depth(state):
    t = 1_000_000.0
    # depth 2: done in 25 min, stopped at the limit after 30, failed after 27
    _job(state, "w1", kind="web", depth=2, state="done", started=t, finished=t + 1500)
    _job(state, "w2", kind="web", depth=2, state="stopped", stop_reason="partial: ...",
         started=t, finished=t + 1800)  # fmt: skip
    _job(state, "w3", kind="web", depth=2, state="failed", error="boom",
         started=t, finished=t + 1620)  # fmt: skip
    # depth 1, revised: both attempts count (the first from history)
    _job(state, "w4", kind="web", depth=1, state="done", started=t + 1000, finished=t + 1600,
         history=[{"state": "done", "started": t, "finished": t + 540}])  # fmt: skip
    # cancelled and still running: not counted
    _job(state, "w5", kind="web", depth=2, state="stopped", stop_reason="cancelled by the caller",
         started=t, finished=t + 5)  # fmt: skip
    _job(state, "w6", kind="web", depth=2, state="running", started=t)
    rec = outcomes.track_record()["web_research"]
    assert rec["runs"] == 5 and rec["median_s"] == 1500
    assert rec["by_depth"] == {1: {"median_s": 600, "runs": 2}, 2: {"median_s": 1620, "runs": 3}}

    from ai_delegate.server import _typical

    assert _typical(rec) == "typically 27 min at depth 2, 10 min at depth 1"


def test_track_record_uses_the_most_recent_runs(state):
    t = 1_000_000.0
    for n in range(25):  # five old slow runs, then twenty quick ones
        secs = 3000 if n < 5 else 600
        _job(state, f"r{n:02d}", kind="research", state="done", started=t + n * 10_000,
             finished=t + n * 10_000 + secs)  # fmt: skip
    for n in range(25):
        outcomes.log_call(f"c-{n:06d}", "ask", "flint", "studio", seconds=100.0 if n < 5 else 4.0)
    rec = outcomes.track_record()
    assert rec["research"] == {"median_s": 600, "runs": 20, "outcomes": {}}
    assert (rec["ask"]["median_s"], rec["ask"]["runs"]) == (4.0, 20)
