import json

import pytest

from ai_delegate import outcomes, server


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_DELEGATE_STATE", str(tmp_path))
    monkeypatch.setattr(server, "_session_charged", False)
    return tmp_path


def ledger(state):
    return [json.loads(x) for x in (state / "tokens.jsonl").read_text().splitlines()]


async def test_session_overhead_once_and_job_calls_charged_to_the_job(state):
    await server.job_status("nope")  # positional, as a direct caller would
    await server.job_status(job_id="nope")
    rows = ledger(state)
    overhead = [r for r in rows if r["tool"] == "overhead"]
    assert len(overhead) == 1 and overhead[0]["cost"] > 1000  # the tool definitions
    charged = [r for r in rows if r["id"] == "nope"]
    assert len(charged) == 2 and all(r["tool"] == "job_status" for r in charged)


async def test_feedback_is_metered_against_its_item(state):
    outcomes.log_call("c-abcdef", "ask", "flint", "studio")
    out = await server.feedback(id="c-abcdef", outcome="kept")
    assert out.startswith("Recorded")
    assert any(r["id"] == "c-abcdef" and r["tool"] == "feedback" for r in ledger(state))
