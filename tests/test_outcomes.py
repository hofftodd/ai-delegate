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
