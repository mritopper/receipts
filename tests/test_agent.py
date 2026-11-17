"""End-to-end runs of the research loop with offline stand-ins (no network, no keys)."""
import pytest

from receipts.agent import research, to_markdown
from receipts.config import Settings
from receipts.llm import LLMError
from receipts.mock import MockLLM, MockTavily
from receipts.search import SearchError


def settings(**kw):
    s = Settings()
    s.mock, s.use_cache = True, False
    for k, v in kw.items():
        setattr(s, k, v)
    return s


async def run(q, llm=None, tavily=None, s=None):
    s = s or settings()
    events = [ev async for ev in research(q, llm or MockLLM(s), tavily or MockTavily(s), s)]
    final = next((e["report"] for e in events if e["type"] == "final"), None)
    return events, final


async def test_full_run_cites_only_verified_quotes():
    events, rep = await run("What is the mock widget standard?")
    assert rep and rep["mock"] is True
    assert rep["stats"]["quotes_rejected"] >= 3  # the reader's fabricated quote is caught on every page
    for s in rep["answer"]:
        assert s["cites"] and all(c in rep["evidence"] for c in s["cites"])
        assert s["label"] in ("supported", "partial")
    for ev in rep["evidence"].values():
        assert ev["fragment_url"].startswith(ev["url"].split("#")[0] + "#:~:text=")
    # the unsupported "mandatory" sentence was removed and is reported, not silently dropped
    assert any("mandatory" in r["text"] for r in rep["removed"])
    assert not any("mandatory" in s["text"] for s in rep["answer"])
    types = [e["type"] for e in events]
    for t in ("start", "plan", "search", "read", "draft", "check", "final"):
        assert t in types
    md = to_markdown(rep)
    assert "[^1]:" in md and "Claims removed by the fact-check" in md


async def test_injection_text_is_flagged():
    _, rep = await run("What is the mock widget standard?")
    flagged = [s for s in rep["sources"] if s["suspicious"]]
    assert flagged and "ignore-previous" in flagged[0]["suspicious"]
    assert not any("ignore previous" in e["quote"].lower() for e in rep["all_evidence"])


async def test_no_results_gives_insufficient_without_writing():
    events, rep = await run("nothing-matches at all")
    assert rep["status"] == "insufficient" and rep["answer"] == []
    assert "Insufficient evidence" in rep["message"]
    assert "draft" not in [e["type"] for e in events]  # no model was asked to improvise


class FailingTavily(MockTavily):
    async def search(self, *a, **k):
        raise SearchError("Tavily HTTP 503")


async def test_search_outage_is_reported_not_hidden():
    events, rep = await run("What is the mock widget standard?", tavily=FailingTavily(settings()))
    assert any(e["type"] == "warning" for e in events)
    assert rep["status"] == "insufficient"
    assert any(f["stage"] == "search" for f in rep["stats"]["failures"])


class FlakyPlanner(MockLLM):
    async def chat_json(self, tier, messages, *, role, ledger, stage="", **kw):
        if role == "planner":
            raise LLMError("unparseable")
        return await super().chat_json(tier, messages, role=role, ledger=ledger, stage=stage, **kw)


async def test_planner_failure_falls_back_to_the_question():
    events, rep = await run("What is the mock widget standard?", llm=FlakyPlanner(settings()))
    plan = next(e for e in events if e["type"] == "plan")
    assert plan["subquestions"][0]["text"] == "What is the mock widget standard?"
    assert rep is not None


class SilentChecker(MockLLM):
    def _checker(self, text):
        return {"results": []}


async def test_unchecked_sentences_are_not_presented_as_supported():
    _, rep = await run("What is the mock widget standard?", llm=SilentChecker(settings()))
    assert all(s["label"] != "supported" for s in rep["answer"])
    assert rep["status"] == "insufficient"  # nothing verified -> do not claim an answer


@pytest.mark.parametrize("q", ["", "hi"])
async def test_rejects_empty_questions(q):
    events, rep = await run(q)
    assert events[0]["type"] == "error" and rep is None
