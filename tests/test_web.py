# ruff: noqa: E501 - the fake model's answers are literal JSON lines
"""Web research: the fetch guard, quote checks, JSON from chatty models, the pipeline, and a
whole web job through the manager, all against a fake web and a fake model."""

import asyncio
import json
import time
from collections import Counter

import httpx
import pytest
from ai_common import web as aiweb
from conftest import models, status
from test_swap import chunk, sse

from ai_delegate import jobs, web
from ai_delegate.config import Config
from ai_delegate.swap import AiSwap

PAGE_A = """<html><head><title>Widget Spec 2.0</title><script>alert('x')</script></head>
<body><nav>menu menu</nav><p>The widget protocol uses port 7070 by default.</p>
<p>Version 2.0 was released in March 2026 by the Widget Group.</p></body></html>"""
PAGE_B = "<html><title>Widget news</title><p>Critics say widgets are “slow – very slow”.</p></html>"
PAGE_C = "<html><title>Widget FAQ</title><p>Widgets can be configured with a YAML file.</p></html>"

SEARCH = {
    "widget protocol port": ["https://spec.example.org/a", "https://evil.example/redirect"],
    "widget release": ["https://spec.example.org/a", "https://news.example.com/b"],
    "widget criticism": ["https://news.example.com/b"],
    "widget config file": ["https://faq.example.net/c", "https://files.example.org/x.pdf"],
}


def web_handler(hits: Counter):
    def h(req: httpx.Request) -> httpx.Response:
        url = str(req.url).split("?")[0]
        hits[url] += 1
        if req.url.host == "search.test":
            q = req.url.params["q"]
            rows = [{"title": u, "url": u, "content": "..."} for u in SEARCH.get(q, [])]
            return httpx.Response(200, json={"results": rows})
        if url == "https://evil.example/redirect":
            return httpx.Response(302, headers={"location": "http://10.0.0.5/secret"})
        if url == "https://files.example.org/x.pdf":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")
        body = {"https://spec.example.org/a": PAGE_A, "https://news.example.com/b": PAGE_B,
                "https://faq.example.net/c": PAGE_C}.get(url)  # fmt: skip
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=body)

    return h


@pytest.fixture(autouse=True)
def public_hosts(monkeypatch):
    """No real DNS in tests: 10.x and *.internal are private, everything else public."""
    monkeypatch.setattr(
        aiweb, "is_public", lambda host: not (host.startswith("10.") or host.endswith(".internal"))
    )


class FakeModel:
    """Answers each pipeline step the way a decent local model would, with some chatter."""

    def __init__(self, followup: bool = True):
        self.calls: list[str] = []
        self.followup = followup
        self.gaps_asked = 0

    def answer(self, system: str, user: str) -> str:
        step = {web.PLAN_SYSTEM: "plan", web.READ_SYSTEM: "read", web.GAPS_SYSTEM: "gaps",
                web.WRITE_SYSTEM: "write"}[system]  # fmt: skip
        self.calls.append(step)
        if step == "plan":
            if "Follow-up:" in user:
                return json.dumps({"subquestions": [
                    {"question": "How is a widget configured?", "queries": ["widget config file"]}]})  # fmt: skip
            return "Sure! Here is the plan:\n```json\n" + json.dumps({"subquestions": [
                {"question": "What port does the widget protocol use?",
                 "queries": ["widget protocol port"]},
                {"question": "When was 2.0 released, and how was it received?",
                 "queries": ["widget release"]},
            ]}) + "\n```"  # fmt: skip
        if step == "read":
            if "spec.example.org" in user:
                return json.dumps({"findings": [
                    {"claim": "Default port is 7070", "quote": "The widget protocol uses port 7070 by default."},
                    {"claim": "Released in March 2026", "quote": "Version 2.0 was released in March 2026"},
                    {"claim": "It is fast", "quote": "Widgets are the fastest protocol ever made."},
                ]})  # fmt: skip
            if "news.example.com" in user:
                return '<think>hmm {"not": "this"}</think>{"findings": [{"claim": "Critics call it slow", "quote": "Critics say widgets are \\"slow - very slow\\"."}]}'
            if "faq.example.net" in user:
                return json.dumps({"findings": [
                    {"claim": "Configured with YAML", "quote": "Widgets can be configured with a YAML file."}]})  # fmt: skip
            return '{"findings": []}'
        if step == "gaps":
            self.gaps_asked += 1
            if self.followup and self.gaps_asked == 1:
                return json.dumps({"followups": [{"sub": 2, "queries": ["widget criticism"]}]})
            return '{"followups": []}'
        return "Widgets use port 7070 [1] and 2.0 shipped in March 2026 [1]. Also [9]."


async def run_pipeline(model: FakeModel, hits: Counter, depth: int = 2, **kw):
    cfg = web.WebConfig(search_url=["http://down.internal:1", "http://search.test"])
    logs: list[str] = []

    async def ask(system, user):
        return model.answer(system, user)

    def fail_first(req):
        if req.url.host == "down.internal":
            raise httpx.ConnectError("no route")
        return web_handler(hits)(req)

    r = web.Research(cfg, ask, logs.append, httpx.MockTransport(fail_first))
    return await r.run("How does the widget protocol work?", depth=depth, **kw), logs


# -- pieces ---------------------------------------------------------------------------------


def test_quotes_are_checked_loosely_but_not_too_loosely():
    page = "Critics say widgets are “slow – very slow”.  Really."
    assert web.quote_in('critics say widgets are "slow - very slow"', page)
    assert web.quote_in("Critics   say\nwidgets are", page)
    assert not web.quote_in("widgets are fast", page)
    assert not web.quote_in("slow", page)  # too short to prove anything


def test_json_from_chatty_replies():
    assert web.parse_json('Here: ```json\n{"a": [1, {"b": "}"}]}\n``` done') == {
        "a": [1, {"b": "}"}]
    }
    assert web.parse_json('<think>{"x": 1}</think> ok {"y": "a \\" b"}') == {"y": 'a " b'}
    assert web.parse_json("[1, 2]") == [1, 2]
    with pytest.raises(ValueError):
        web.parse_json("no json here")


def test_html_to_text_drops_chrome():
    title, text = web.to_text(PAGE_A)
    assert title == "Widget Spec 2.0"
    assert "port 7070" in text and "alert" not in text and "menu" not in text


async def test_fetch_guard():
    hits: Counter = Counter()
    t = httpx.MockTransport(web_handler(hits))
    cfg = web.WebConfig()
    ok = await web.fetch("https://spec.example.org/a", cfg, t)
    assert ok.ok and ok.title == "Widget Spec 2.0"
    bad = await web.fetch("https://evil.example/redirect", cfg, t)
    assert not bad.ok and "not a public address" in bad.error
    assert hits["http://10.0.0.5/secret"] == 0  # refused before any request went out
    assert "not a text page" in (await web.fetch("https://files.example.org/x.pdf", cfg, t)).error
    assert "only http" in (await web.fetch("file:///etc/passwd", cfg, t)).error
    assert "not a public" in (await web.fetch("http://db.internal/", cfg, t)).error


async def test_search_fails_over_then_reports():
    hits: Counter = Counter()

    def h(req):
        if req.url.host == "down.internal":
            raise httpx.ConnectError("no route")
        return web_handler(hits)(req)

    cfg = web.WebConfig(search_url=["http://down.internal:1", "http://search.test"])
    got = await web.search(cfg, "widget release", httpx.MockTransport(h))
    assert [x.url for x in got] == SEARCH["widget release"]
    cfg.search_url = ["http://down.internal:1"]
    with pytest.raises(web.ResearchError, match="search failed"):
        await web.search(cfg, "x", httpx.MockTransport(h))


# -- the pipeline ---------------------------------------------------------------------------


async def test_pipeline_end_to_end():
    model, hits = FakeModel(), Counter()
    res, logs = await run_pipeline(model, hits)
    st = res.stats()
    # Two plan subs, a follow-up search for sub 2 (depth 2), the write.
    assert model.calls[0] == "plan" and model.calls[-1] == "write" and "gaps" in model.calls
    assert st["subquestions"] == 2 and st["searches"] == 3
    assert st["verified"] == 3 and st["unverified"] == 1  # the invented "fastest" quote
    assert [f.claim for f in res.findings if not f.verified] == ["It is fast"]
    assert res.sources() == ["https://spec.example.org/a", "https://news.example.com/b"]
    assert hits["https://spec.example.org/a"] == 1  # found by two queries, read once
    assert st["pages_failed"] == 1  # the redirect to a private address
    assert res.bad_citations == [9]
    assert res.report.endswith(
        "## Sources\n\n[1] Widget Spec 2.0 — https://spec.example.org/a\n\n"
        "Also read, with verified findings, but not cited:\n\n"
        "[2] Widget news — https://news.example.com/b"
    )
    assert any("gaps: more searching for sub(s) 2" in m for m in logs)


async def test_depth_one_skips_gap_search():
    model, hits = FakeModel(), Counter()
    res, _ = await run_pipeline(model, hits, depth=1)
    assert "gaps" not in model.calls and res.stats()["searches"] == 2


async def test_no_verified_findings_means_no_report():
    class Liar(FakeModel):
        def answer(self, system, user):
            if system == web.READ_SYSTEM:
                self.calls.append("read")
                return '{"findings": [{"claim": "x", "quote": "words that are on no page at all"}]}'
            return super().answer(system, user)

    model = Liar(followup=False)
    res, _ = await run_pipeline(model, Counter())
    assert "No verified findings" in res.report and "write" not in model.calls


async def test_followup_reuses_pages():
    model, hits = FakeModel(followup=False), Counter()
    first, _ = await run_pipeline(model, hits)
    reads_before = hits["https://spec.example.org/a"]
    second, _ = await run_pipeline(
        model, hits, followup="How is it configured?", previous=first,
        previous_report=first.report,
    )  # fmt: skip
    assert len(second.subs) == 3 and second.subs[2].question == "How is a widget configured?"
    assert hits["https://spec.example.org/a"] == reads_before  # not fetched again
    assert "https://faq.example.net/c" in second.sources()


async def test_result_saves_and_loads(tmp_path):
    res, _ = await run_pipeline(FakeModel(), Counter())
    res.save(tmp_path / "web.json")
    back = web.Result.load(tmp_path / "web.json")
    assert back.stats() == res.stats() and back.report == res.report
    assert (
        back.pages["https://spec.example.org/a"].text
        == res.pages["https://spec.example.org/a"].text
    )


# -- a whole web job through the manager ------------------------------------------------------


@pytest.fixture
def web_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("AI_DELEGATE_STATE", str(tmp_path / "state"))
    model, hits = FakeModel(), Counter()

    def swap_api(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/status":
            return httpx.Response(200, json=status())
        if req.url.path == "/v1/models":
            return httpx.Response(200, json=models())
        body = json.loads(req.content)
        msgs = body["messages"]
        text = model.answer(msgs[0]["content"], msgs[1]["content"])
        usage = {"usage": {"prompt_tokens": 100, "completion_tokens": 20}, "choices": []}
        return httpx.Response(200, text=sse(chunk(content=text, finish="stop"), usage))

    cfg = Config(endpoint="http://swap")
    cfg.web.search_url = ["http://search.test"]
    m = jobs.Manager(cfg, AiSwap("http://swap", transport=httpx.MockTransport(swap_api)))
    m.web_transport = httpx.MockTransport(web_handler(hits))
    return m, model, hits


async def wait_job(m, job_id):
    for _ in range(300):
        j = m.get(job_id)
        if j.state not in jobs.ACTIVE:
            return j
        await asyncio.sleep(0.02)
    raise AssertionError("job did not finish")


async def test_web_job(web_manager, tmp_path):
    m, model, _ = web_manager
    job = await m.start(
        "How does the widget protocol work?", "", tmp_path, model=None, test_command=None,
        include_uncommitted=True, time_limit_s=60, sandbox_mode=None, kind="web", depth=2,
    )  # fmt: skip
    assert job.kind == "web" and job.sandbox == "none" and job.repo == "" and job.model == "flint"
    done = await wait_job(m, job.id)
    assert done.state == "done", done.error
    assert done.citations["verified"] == 3 and done.requests == len(model.calls)
    assert done.new_tokens == 120 * done.requests
    assert "## Sources" in (done.dir / "report.md").read_text()
    assert (done.dir / "web.json").is_file()

    revised = await m.revise(job.id, "How is it configured?", 60)
    assert revised.attempt == 2
    again = await wait_job(m, job.id)
    assert again.state == "done", again.error
    assert again.citations["sources"] == 3  # the FAQ page joined the earlier two


async def test_web_job_stops_at_the_call_limit(web_manager, tmp_path):
    m, _, _ = web_manager
    m.cfg.jobs.max_requests = 2
    job = await m.start(
        "q", "", tmp_path, model=None, test_command=None, include_uncommitted=True,
        time_limit_s=60, sandbox_mode=None, kind="web",
    )  # fmt: skip
    done = await wait_job(m, job.id)
    assert done.state == "stopped" and not done.error
    assert (
        done.stop_reason
        == "partial: stopped at the model call limit (2) after 0 of 2 sub-question(s)"
    )
    assert done.citations["subquestions"] == 2  # what it got before the limit is kept
    assert "No verified findings" in (done.dir / "report.md").read_text()


async def test_web_job_writes_a_partial_report_at_the_call_limit(web_manager, tmp_path):
    m, model, _ = web_manager
    m.cfg.jobs.max_requests = 3  # plan, one read, and the write kept back
    job = await m.start(
        "q", "", tmp_path, model=None, test_command=None, include_uncommitted=True,
        time_limit_s=60, sandbox_mode=None, kind="web",
    )  # fmt: skip
    done = await wait_job(m, job.id)
    assert done.state == "stopped", done.error
    assert (
        done.stop_reason
        == "partial: stopped at the model call limit (3) after 1 of 2 sub-question(s)"
    )
    assert model.calls == ["plan", "read", "write"] and done.requests == 3
    report = (done.dir / "report.md").read_text()
    assert report.startswith("> **partial: stopped") and "## Sources" in report


async def test_web_job_writes_a_partial_report_at_the_time_limit(web_manager, tmp_path):
    m, model, _ = web_manager
    answer = model.answer

    def slow_plan(system, user):
        if system == web.PLAN_SYSTEM:
            time.sleep(1.6)  # past the soft deadline: 2 s less a quarter kept for the write
        return answer(system, user)

    model.answer = slow_plan
    job = await m.start(
        "q", "", tmp_path, model=None, test_command=None, include_uncommitted=True,
        time_limit_s=2, sandbox_mode=None, kind="web",
    )  # fmt: skip
    done = await wait_job(m, job.id)
    assert done.state == "stopped", done.error
    assert (
        done.stop_reason == "partial: stopped at the time limit (2 s) after 0 of 2 sub-question(s)"
    )
    assert model.calls == ["plan"]  # no search after the soft deadline; nothing to write
    assert (done.dir / "report.md").is_file() and (done.dir / "web.json").is_file()
