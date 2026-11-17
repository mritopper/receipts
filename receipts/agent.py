"""The research loop.

question
  -> PLAN       (fast model)    sub-questions, each with a search query and a counter-evidence query
  -> SEARCH     (Tavily)        live results with page text; canonical-URL + near-duplicate dedupe
  -> READ       (fast model)    verbatim quotes per page  -> locate_quote() drops any quote not on the page
  -> COVERAGE   (strong model)  per-sub-question audit; follow-up searches or stop (max RC_MAX_ROUNDS)
  -> WRITE      (strong model)  sentence-level answer citing evidence ids; conflicts; gaps
  -> CHECK      (check model + deterministic number check)  supported / partial / unsupported per sentence
  -> REPAIR     (strong model)  rewrite or drop failing sentences, re-check; still failing -> removed, shown
  -> REPORT     every surviving sentence carries receipts: quote, page, date, and a link that opens the page
                at the quoted passage.

`research()` is an async generator of events so the UI and CLI can show the trace live.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import date
from typing import Any, AsyncIterator

from . import prompts
from .config import Settings
from .llm import Ledger, LLMError
from .safety import scan_injection, wrap_source
from .search import SearchError, Source, SourcePool
from .verify import fragment_url, locate_quote, missing_numbers, normalise

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_CITE_MARK = re.compile(r"\s*\[(?:E\d+(?:\s*[,;]\s*)?)+\]")


def _today() -> str:
    return date.today().isoformat()


# --------------------------------------------------------------------------- plan

def _clean_plan(raw: Any, question: str, s: Settings) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    subs = []
    for i, sq in enumerate((raw.get("subquestions") or [])[: s.max_subquestions]):
        if not isinstance(sq, dict):
            continue
        text = str(sq.get("text") or "").strip()
        query = str(sq.get("query") or text).strip()
        if not query:
            continue
        subs.append({
            "id": f"Q{len(subs) + 1}",
            "text": text or query,
            "query": query[:300],
            "check_query": str(sq.get("check_query") or "").strip()[:300],
        })
    if not subs:
        subs = [{"id": "Q1", "text": question, "query": question[:300], "check_query": ""}]
    tr = raw.get("time_range")
    return {
        "question_type": str(raw.get("question_type") or "other"),
        "needs_fresh": bool(raw.get("needs_fresh")),
        "time_range": tr if tr in ("day", "week", "month", "year") else None,
        "topic": raw.get("topic") if raw.get("topic") in ("general", "news", "finance") else "general",
        "subquestions": subs,
    }


def _fallback_plan(question: str) -> dict:
    return {
        "question_type": "other", "needs_fresh": False, "time_range": None, "topic": "general",
        "subquestions": [{"id": "Q1", "text": question, "query": question[:300],
                          "check_query": f"{question[:200]} criticism OR dispute OR correction"}],
    }


# --------------------------------------------------------------------------- the run

class Run:
    def __init__(self, question: str, llm, tavily, s: Settings):
        self.q = question.strip()
        self.llm, self.tavily, self.s = llm, tavily, s
        self.ledger = Ledger()
        self.pool = SourcePool(s.max_sources, s.source_chars)
        self.evidence: dict[str, dict] = {}
        self._seen_quotes: set[str] = set()
        self.rejected: list[dict] = []
        self.queries: list[dict] = []
        self.failures: list[dict] = []
        self.plan: dict = {}
        self.t0 = time.perf_counter()
        self.today = _today()

    def meter(self) -> dict:
        return {"type": "meter", **self.ledger.summary(), "elapsed_s": round(time.perf_counter() - self.t0, 1)}

    # ---- search ---------------------------------------------------------------------
    async def _one_search(self, q: dict) -> dict:
        t = time.perf_counter()
        tr = self.plan.get("time_range") if q["kind"] != "check" else None
        topic = self.plan.get("topic", "general")
        out = {**q, "status": "ok", "n_results": 0, "cached": False, "broadened": False, "results": []}
        try:
            results, credits, cached = await self.tavily.search(
                q["query"], depth=q["depth"], max_results=self.s.results_per_query, topic=topic, time_range=tr
            )
            if not results and (tr or topic != "general"):
                # Empty results under a date or topic filter: broaden once rather than give up.
                results, c2, cached = await self.tavily.search(
                    q["query"], depth=q["depth"], max_results=self.s.results_per_query
                )
                credits += c2
                out["broadened"] = True
            self.ledger.tavily_calls += 1 + int(out["broadened"])
            self.ledger.tavily_credits += credits
            out.update(results=results, n_results=len(results), cached=cached,
                       status="ok" if results else "empty")
        except SearchError as e:
            out.update(status="error", error=str(e)[:200])
            self.failures.append({"stage": "search", "query": q["query"], "error": str(e)[:200]})
        out["ms"] = int((time.perf_counter() - t) * 1000)
        return out

    async def search_round(self, batch: list[dict], round_no: int) -> AsyncIterator[dict]:
        done = await asyncio.gather(*(self._one_search(q) for q in batch))
        for res in done:  # add in query order so source ids are stable
            results = res.pop("results", [])
            new = self.pool.add(results, res["id"]) if res["status"] == "ok" else []
            self.queries.append({k: res[k] for k in ("id", "subq", "kind", "query", "status", "n_results")})
            yield {"type": "search", "round": round_no, **res, "new_sources": [s.public() for s in new]}
            res["_new"] = new
        self._last_new = [s for r in done for s in r.get("_new", [])]

    # ---- read -----------------------------------------------------------------------
    def _subq_text(self) -> str:
        return "\n".join(f"{sq['id']}: {sq['text']}" for sq in self.plan["subquestions"])

    async def _read_one(self, src: Source) -> tuple[Source, dict | None, str]:
        if len(src.text) < 80:
            return src, None, "page text too short"
        msgs = [
            {"role": "system", "content": prompts.EXTRACTOR.format(subqs=self._subq_text())},
            {"role": "user", "content": wrap_source(src.id, src.title, src.url, src.published, src.text)},
        ]
        try:
            data, _ = await self.llm.chat_json("fast", msgs, role="reader", stage=f"read {src.id}",
                                               ledger=self.ledger, max_tokens=1100, temperature=0.0)
            return src, data if isinstance(data, dict) else {}, ""
        except LLMError as e:
            self.failures.append({"stage": "read", "source": src.id, "error": str(e)[:200]})
            return src, None, "reader failed"

    def _accept(self, src: Source, data: dict) -> tuple[list[dict], list[dict]]:
        valid_q = {sq["id"] for sq in self.plan["subquestions"]}
        pub = data.get("published")
        if not src.published and isinstance(pub, str) and _DATE.match(pub):
            src.published = pub[:10]
        acc, rej = [], []
        for item in (data.get("quotes") or [])[:5]:
            if not isinstance(item, dict):
                continue
            quote = str(item.get("quote") or "").strip()
            if len(quote) < 15:
                continue
            if scan_injection(quote):
                rej.append({"source_id": src.id, "quote": quote[:300], "reason": "looks like an instruction to the model"})
                continue
            m = locate_quote(quote, src.text)
            if not m:
                rej.append({"source_id": src.id, "quote": quote[:300], "reason": "not found on the page"})
                continue
            text = m.text if len(m.text) <= 600 else m.text[:600].rsplit(" ", 1)[0] + "..."
            key = normalise(text)
            if key in self._seen_quotes:
                continue
            self._seen_quotes.add(key)
            eid = f"E{len(self.evidence) + 1}"
            ev = {
                "id": eid, "source_id": src.id, "quote": text, "claim": str(item.get("claim") or "")[:200],
                "subq": [x for x in (item.get("subq") or []) if x in valid_q] or [],
                "url": src.url, "fragment_url": fragment_url(src.url, text), "title": src.title,
                "domain": src.domain, "tier": src.tier, "published": src.published,
                "match": m.method, "match_score": m.score,
            }
            self.evidence[eid] = ev
            acc.append(ev)
        return acc, rej

    async def read_sources(self, sources: list[Source]) -> AsyncIterator[dict]:
        tasks = [asyncio.ensure_future(self._read_one(s)) for s in sources]
        for fut in asyncio.as_completed(tasks):
            src, data, err = await fut
            if data is None:
                yield {"type": "read", "source_id": src.id, "domain": src.domain, "accepted": [], "rejected": [],
                       "error": err, "suspicious": src.suspicious}
                continue
            acc, rej = self._accept(src, data)
            self.rejected.extend(rej)
            yield {"type": "read", "source_id": src.id, "domain": src.domain, "accepted": acc, "rejected": rej,
                   "suspicious": src.suspicious, "published": src.published}

    # ---- coverage -------------------------------------------------------------------
    def _ledger_brief(self) -> str:
        lines = []
        for ev in self.evidence.values():
            lines.append(f"{ev['id']} | {ev['domain']} | {ev['tier']} | {ev['published'] or 'undated'} | "
                         f"{','.join(ev['subq']) or '-'} | {ev['claim'] or ev['quote'][:160]}")
        return "\n".join(lines) or "(no evidence yet)"

    def _det_coverage(self) -> list[dict]:
        cov = []
        for sq in self.plan["subquestions"]:
            evs = [e for e in self.evidence.values() if sq["id"] in e["subq"]]
            domains = {e["domain"] for e in evs}
            primary = any(e["tier"] == "primary" for e in evs)
            status = "missing" if not evs else ("answered" if len(domains) >= 2 or primary else "thin")
            cov.append({"subq": sq["id"], "status": status, "evidence": len(evs), "domains": len(domains)})
        return cov

    async def coverage(self, round_no: int) -> dict:
        det = self._det_coverage()
        ran = {normalise(q["query"]) for q in self.queries}
        try:
            data, _ = await self.llm.chat_json(
                "strong",
                [{"role": "user", "content": prompts.COVERAGE.format(
                    today=self.today, question=self.q, subqs=self._subq_text(), max_followups=3,
                    queries="\n".join(f"- {q['query']}" for q in self.queries), ledger=self._ledger_brief())}],
                role="coverage", stage=f"coverage r{round_no}", ledger=self.ledger, max_tokens=900, temperature=0.1)
            data = data if isinstance(data, dict) else {}
        except LLMError as e:
            self.failures.append({"stage": "coverage", "error": str(e)[:200]})
            data = {}
        follow = []
        for f in data.get("followups") or []:
            if not isinstance(f, dict) or not f.get("query"):
                continue
            if normalise(str(f["query"])) in ran:
                continue
            follow.append({"subq": str(f.get("subq") or "Q1"), "query": str(f["query"])[:300], "why": str(f.get("why") or "")[:200]})
        if not data:  # model unavailable: deterministic fallback, re-ask missing sub-questions verbatim
            follow = [{"subq": c["subq"], "query": next(sq["text"] for sq in self.plan["subquestions"] if sq["id"] == c["subq"]),
                       "why": "no evidence yet"} for c in det if c["status"] == "missing"]
        follow = follow[:3]
        all_answered = all(c["status"] == "answered" for c in det)
        stop = bool(data.get("stop")) or not follow or (all_answered and not any(
            (c.get("status") == "conflicting") for c in (data.get("coverage") or []) if isinstance(c, dict)))
        reason = str(data.get("stop_reason") or "") or ("every sub-question has independent support" if all_answered
                                                          else "no better query to try")
        return {"type": "coverage", "round": round_no, "deterministic": det,
                "model": [c for c in (data.get("coverage") or []) if isinstance(c, dict)],
                "followups": follow, "stop": stop, "stop_reason": reason[:200]}

    # ---- write / check / repair -----------------------------------------------------
    def _ledger_full(self) -> str:
        out = []
        for ev in self.evidence.values():
            out.append(f'<evidence id="{ev["id"]}" domain="{ev["domain"]}" tier="{ev["tier"]}" '
                       f'published="{ev["published"] or "unknown"}" subq="{",".join(ev["subq"])}">'
                       f'{ev["quote"]}</evidence>')
        return "\n".join(out)

    def _clean_sentences(self, items: Any) -> list[dict]:
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            text = _CITE_MARK.sub("", str(it.get("text") or "")).strip()
            if not text:
                continue
            raw = it.get("cites") or []
            raw = raw if isinstance(raw, list) else [raw]
            cites = [c for c in dict.fromkeys(str(c).strip() for c in raw) if c in self.evidence]
            bad = [str(c) for c in raw if str(c).strip() not in self.evidence]
            kind = "inference" if it.get("kind") == "inference" else "fact"
            out.append({"text": text, "cites": cites, "kind": kind, "invalid_cites": bad})
        return out

    async def check(self, sentences: list[dict], idxs: list[int]) -> None:
        """Label sentences[i] for i in idxs. Deterministic checks first, then the independent checker model."""
        to_model = []
        for i in idxs:
            s = sentences[i]
            quotes = [self.evidence[c]["quote"] for c in s["cites"]]
            s["missing_numbers"] = missing_numbers(s["text"], quotes)
            if not s["cites"]:
                s.update(label="unsupported", reason="no valid citation", checked_by="rule")
            else:
                to_model.append(i)
        for chunk in (to_model[k:k + 8] for k in range(0, len(to_model), 8)):
            items = []
            for j, i in enumerate(chunk):
                s = sentences[i]
                qs = "\n".join(f'  <evidence id="{c}">{self.evidence[c]["quote"]}</evidence>' for c in s["cites"])
                items.append(f"[{j}] ({s['kind']}) {s['text']}\n{qs}")
            try:
                data, _ = await self.llm.chat_json(
                    "check", [{"role": "user", "content": prompts.CHECKER.format(items="\n\n".join(items))}],
                    role="checker", stage="check", ledger=self.ledger, max_tokens=900, temperature=0.0)
                res = {int(r.get("i")): r for r in (data.get("results") or []) if isinstance(r, dict) and str(r.get("i", "")).lstrip("-").isdigit()}
            except (LLMError, AttributeError) as e:
                self.failures.append({"stage": "check", "error": str(e)[:200]})
                res = {}
            for j, i in enumerate(chunk):
                r = res.get(j) or {}
                label = r.get("label") if r.get("label") in ("supported", "partial", "unsupported") else "unchecked"
                sentences[i].update(label=label, reason=str(r.get("reason") or "checker gave no verdict")[:200],
                                    checked_by="model")
        for i in idxs:  # a figure the quotes never mention caps the label at partial
            s = sentences[i]
            if s.get("missing_numbers") and s.get("label") == "supported" and s["kind"] == "fact":
                s.update(label="partial", reason=f"figure(s) {', '.join(s['missing_numbers'])} not in the cited quotes")

    async def repair(self, sentences: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
        flagged = [i for i, s in enumerate(sentences) if s.get("label") in ("unsupported", "partial")]
        removed: list[dict] = []
        if not flagged:
            return sentences, removed, []
        brief = "\n".join(f"[{i}] ({sentences[i]['kind']}, cites {','.join(sentences[i]['cites']) or 'none'}) "
                          f"{sentences[i]['text']}\n    checker: {sentences[i]['label']}: {sentences[i].get('reason', '')}"
                          for i in flagged)
        try:
            data, _ = await self.llm.chat_json(
                "strong", [{"role": "user", "content": prompts.REPAIR.format(
                    question=self.q, flagged=brief, ledger=self._ledger_full())}],
                role="repair", stage="repair", ledger=self.ledger, max_tokens=1400, temperature=0.1)
            revs = [r for r in (data.get("revisions") or []) if isinstance(r, dict)]
        except (LLMError, AttributeError) as e:
            self.failures.append({"stage": "repair", "error": str(e)[:200]})
            revs = []
        log = []
        recheck = []
        for r in revs:
            try:
                i = int(r.get("i"))
            except (TypeError, ValueError):
                continue
            if i not in flagged:
                continue
            before = dict(sentences[i])
            if r.get("action") == "drop":
                sentences[i]["_drop"] = True
                log.append({"i": i, "action": "drop", "before": before["text"]})
                continue
            new = self._clean_sentences([{"text": r.get("text"), "cites": r.get("cites"), "kind": r.get("kind") or before["kind"]}])
            if new:
                sentences[i] = {**new[0], "revised_from": before["text"], "first_label": before.get("label")}
                recheck.append(i)
                log.append({"i": i, "action": "rewrite", "before": before["text"], "after": new[0]["text"]})
        if recheck:
            await self.check(sentences, recheck)
        kept = []
        for s in sentences:
            if s.get("_drop"):
                removed.append({"text": s["text"], "reason": f"dropped in repair ({s.get('label')}: {s.get('reason', '')})"})
            elif s.get("label") == "unsupported":
                removed.append({"text": s["text"], "reason": f"still unsupported after repair: {s.get('reason', '')}"})
            else:
                kept.append(s)
        return kept, removed, log


def _final_status(writer_status: str, kept: list[dict]) -> str:
    facts_ok = [s for s in kept if s["kind"] == "fact" and s.get("label") == "supported"]
    if writer_status == "insufficient" or not facts_ok:
        return "insufficient"
    if writer_status == "partial" or any(s.get("label") != "supported" for s in kept):
        return "partial"
    return "answered"


async def research(question: str, llm, tavily, s: Settings) -> AsyncIterator[dict]:
    run = Run(question, llm, tavily, s)
    if len(run.q) < 4:
        yield {"type": "error", "message": "Ask a full question."}
        return
    models = await llm.discover()
    yield {"type": "start", "question": run.q, "today": run.today, "models": dict(models),
           "mock": bool(getattr(llm, "is_mock", False))}

    # 1. PLAN
    try:
        raw, _ = await llm.chat_json(
            "fast", [{"role": "system", "content": prompts.PLANNER.format(today=run.today, max_subq=s.max_subquestions)},
                     {"role": "user", "content": run.q}],
            role="planner", stage="plan", ledger=run.ledger, max_tokens=900, temperature=0.2)
        run.plan = _clean_plan(raw, run.q, s)
    except LLMError as e:
        run.failures.append({"stage": "plan", "error": str(e)[:200]})
        run.plan = _fallback_plan(run.q)
    yield {"type": "plan", **run.plan}
    yield run.meter()

    # 2-4. SEARCH -> READ -> COVERAGE, up to max_rounds
    batch = []
    for sq in run.plan["subquestions"]:
        batch.append({"id": f"{sq['id']}.main", "subq": sq["id"], "kind": "main", "query": sq["query"], "depth": "advanced"})
        if sq.get("check_query"):
            batch.append({"id": f"{sq['id']}.check", "subq": sq["id"], "kind": "check", "query": sq["check_query"], "depth": "basic"})
    stop_reason = ""
    for round_no in range(1, s.max_rounds + 1):
        async for ev in run.search_round(batch, round_no):
            yield ev
        new_sources = run._last_new
        if all(q["status"] == "error" for q in run.queries[-len(batch):]):
            yield {"type": "warning", "message": "Every search in this round failed. Results may be incomplete."}
        async for ev in run.read_sources(new_sources):
            yield ev
        yield run.meter()
        if round_no == s.max_rounds:
            stop_reason = "search budget reached"
            break
        if not new_sources and round_no > 1:
            stop_reason = "follow-up searches found no new pages"
            break
        cov = await run.coverage(round_no)
        yield cov
        yield run.meter()
        if cov["stop"]:
            stop_reason = cov["stop_reason"]
            break
        batch = [{"id": f"F{round_no}.{k + 1}", "subq": f["subq"], "kind": "followup", "query": f["query"],
                  "why": f["why"], "depth": "advanced"} for k, f in enumerate(cov["followups"])]

    yield {"type": "stop", "reason": stop_reason, "evidence": len(run.evidence), "sources": len(run.pool)}

    # No usable evidence at all: say so instead of asking a model to improvise.
    if not run.evidence:
        report = _report(run, {"status": "insufficient", "confidence": "low",
                               "confidence_reason": "no page returned a verifiable passage"}, [], [], [], [])
        report["message"] = ("Insufficient evidence. The searches returned no page with a passage that answers this "
                             "question, so no answer is given. See the trace for every query that was run.")
        yield {"type": "final", "report": report}
        return

    # 5. WRITE
    try:
        draft, _ = await llm.chat_json(
            "strong", [{"role": "user", "content": prompts.WRITER.format(
                today=run.today, question=run.q, ledger=run._ledger_full())}],
            role="writer", stage="write", ledger=run.ledger, max_tokens=2200, temperature=0.2)
        draft = draft if isinstance(draft, dict) else {}
    except LLMError as e:
        run.failures.append({"stage": "write", "error": str(e)[:200]})
        yield {"type": "error", "message": f"The writer model failed: {e}. Evidence was gathered; try again."}
        return
    sentences = run._clean_sentences(draft.get("answer"))
    yield {"type": "draft", "status": draft.get("status"), "answer": sentences}
    yield run.meter()

    # 6. CHECK
    await run.check(sentences, list(range(len(sentences))))
    yield {"type": "check", "pass": 1, "answer": [dict(x) for x in sentences]}
    first_pass = [x.get("label") for x in sentences]

    # 7. REPAIR
    kept, removed, log = await run.repair(sentences)
    if log or removed:
        yield {"type": "repair", "revisions": log, "removed": removed}
    yield run.meter()

    conflicts = []
    for c in draft.get("conflicts") or []:
        if not isinstance(c, dict):
            continue
        pos = []
        for p in c.get("positions") or []:
            if isinstance(p, dict):
                cites = [x for x in (p.get("cites") or []) if x in run.evidence]
                if cites:
                    pos.append({"claim": str(p.get("claim") or "")[:300], "cites": cites})
        if len(pos) >= 2:
            conflicts.append({"point": str(c.get("point") or "")[:300], "positions": pos, "why": str(c.get("why") or "")[:300]})
    gaps = [str(g)[:300] for g in (draft.get("gaps") or []) if str(g).strip()][:6]
    report = _report(run, draft, kept, removed, conflicts, gaps, first_pass=first_pass, repairs=log)
    yield run.meter()
    yield {"type": "final", "report": report}


def _report(run: Run, draft: dict, kept: list[dict], removed: list[dict], conflicts: list[dict], gaps: list[str],
            first_pass: list | None = None, repairs: list | None = None) -> dict:
    status = _final_status(str(draft.get("status") or ""), kept) if kept else "insufficient"
    used = sorted({c for s in kept for c in s["cites"]} | {c for k in conflicts for p in k["positions"] for c in p["cites"]},
                  key=lambda e: int(e[1:]))
    supported = sum(1 for s in kept if s.get("label") == "supported")
    conf = str(draft.get("confidence") or "low")
    reason = str(draft.get("confidence_reason") or "")
    total_written = len(kept) + len(removed)
    if total_written and supported / total_written < 0.6 and conf == "high":
        conf, reason = "medium", (reason + " Capped: under 60% of drafted sentences passed the fact-check.").strip()
    if status == "insufficient":
        conf = "low"
    domains = {run.evidence[e]["domain"] for e in used}
    dated = sorted([run.evidence[e]["published"] for e in used if run.evidence[e]["published"]])
    stats = {
        "sources_found": len(run.pool), "duplicates_skipped": run.pool.dupes,
        "quotes_proposed": len(run.evidence) + len(run.rejected), "quotes_verified": len(run.evidence),
        "quotes_rejected": len(run.rejected), "evidence_cited": len(used), "domains_cited": len(domains),
        "primary_cited": sum(1 for e in used if run.evidence[e]["tier"] == "primary"),
        "sentences_drafted": len(first_pass or []), "sentences_supported_first_pass": sum(1 for x in (first_pass or []) if x == "supported"),
        "sentences_final": len(kept), "sentences_supported_final": supported, "sentences_removed": len(removed),
        "newest_source": dated[-1] if dated else None, "oldest_source": dated[0] if dated else None,
        "searches": len(run.queries), "failures": run.failures,
        "elapsed_s": round(time.perf_counter() - run.t0, 1), **run.ledger.summary(),
    }
    return {
        "question": run.q, "asked_on": run.today, "status": status, "confidence": conf, "confidence_reason": reason,
        "answer": kept, "removed": removed, "repairs": repairs or [], "conflicts": conflicts, "gaps": gaps,
        "evidence": {e: run.evidence[e] for e in used},
        "all_evidence": list(run.evidence.values()), "rejected_quotes": run.rejected,
        "sources": [s.public() for s in run.pool.all()], "plan": run.plan, "queries": run.queries,
        "models": dict(run.llm.models), "stats": stats, "mock": bool(getattr(run.llm, "is_mock", False)),
    }


def to_markdown(report: dict) -> str:
    """Portable copy of an answer: sentences with footnotes, each footnote carrying its verbatim quote."""
    order: list[str] = []
    for s in report["answer"]:
        for c in s["cites"]:
            if c not in order:
                order.append(c)
    num = {e: i + 1 for i, e in enumerate(order)}
    lines = [f"# {report['question']}", ""]
    if report.get("message"):
        lines += [report["message"], ""]
    status = {"answered": "Answered", "partial": "Partly answered", "insufficient": "Insufficient evidence"}[report["status"]]
    lines += [f"*{status} · confidence {report['confidence']} · researched {report['asked_on']}*", ""]
    body = []
    for s in report["answer"]:
        marks = "".join(f"[^{num[c]}]" for c in s["cites"])
        tag = " *(inference)*" if s["kind"] == "inference" else ""
        warn = " *(partly supported)*" if s.get("label") == "partial" else ""
        body.append(f"{s['text']}{tag}{warn}{marks}")
    lines += [" ".join(body), ""]
    if report["conflicts"]:
        lines += ["## Where sources disagree", ""]
        for c in report["conflicts"]:
            lines.append(f"- **{c['point']}** " + " vs. ".join(
                f"{p['claim']} ({', '.join(report['evidence'][x]['domain'] for x in p['cites'] if x in report['evidence'])})"
                for p in c["positions"]) + (f" — {c['why']}" if c.get("why") else ""))
        lines.append("")
    if report["gaps"]:
        lines += ["## Not established by the evidence", ""] + [f"- {g}" for g in report["gaps"]] + [""]
    if report["removed"]:
        lines += ["## Claims removed by the fact-check", ""] + [f"- ~~{r['text']}~~ ({r['reason']})" for r in report["removed"]] + [""]
    for e in order:
        ev = report["evidence"][e]
        lines.append(f'[^{num[e]}]: "{ev["quote"]}" — [{ev["title"]}]({ev["fragment_url"]}) '
                     f'({ev["domain"]}{", " + ev["published"] if ev["published"] else ""})')
    return "\n".join(lines).rstrip() + "\n"
