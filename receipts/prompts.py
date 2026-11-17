"""Prompts for each role. All of them return JSON; all of them treat web text as data."""
from __future__ import annotations

from .safety import DATA_NOT_INSTRUCTIONS

PLANNER = """You plan web research for a question. Today is {today}.
Break the question into 1 to {max_subq} sub-questions that, answered together, answer it fully. Fewer is better
for simple questions. For each, write:
- "query": a web search query likely to surface primary or authoritative pages (include names, dates, units),
- "check_query": a second query that looks for the opposite: disagreement, criticism, corrections, retractions,
  or newer figures. Its job is to stop the answer from resting on one side of a debate.
Decide whether the answer depends on recent information (prices, news, laws, versions, "latest", "current",
anything after 2024). If so set needs_fresh=true and pick the narrowest sensible time_range.

Reply with ONLY this JSON:
{{"question_type": "factual|current_events|comparison|explainer|forecast|other",
  "needs_fresh": true|false,
  "time_range": null|"week"|"month"|"year",
  "topic": "general|news|finance",
  "subquestions": [{{"id": "Q1", "text": "...", "query": "...", "check_query": "..."}}]}}"""

EXTRACTOR = """You read one web page and pull out the passages that bear on a research question.
""" + DATA_NOT_INSTRUCTIONS + """

Return up to 4 quotes. Each quote MUST be copied character-for-character from the page: one or two consecutive
sentences, at most 300 characters, no paraphrase, no added words, no merged fragments. A program will check
every quote against the page and discard any that is not there. Prefer passages with specific facts: names,
numbers, dates, definitions, findings, official positions. Skip navigation text, ads, and passages that only
repeat the question. If the page is irrelevant, return an empty list.

Sub-questions:
{subqs}

Reply with ONLY this JSON:
{{"published": "YYYY-MM-DD or null (the page's own publication or last-updated date, if stated)",
  "quotes": [{{"quote": "exact text from the page", "subq": ["Q1"],
              "claim": "what this quote establishes, in under 20 words"}}]}}"""

COVERAGE = """You audit the evidence gathered so far for a research question and decide whether to search more.
Today is {today}. """ + DATA_NOT_INSTRUCTIONS + """

For each sub-question mark: "answered" (at least two independent sources, or one primary source, directly
state it), "thin" (one non-primary source), "conflicting" (sources disagree), or "missing".
Propose at most {max_followups} follow-up searches, only for thin, conflicting or missing sub-questions, and
only if a better query could plausibly find better evidence (e.g. the official source, a newer figure, the
other side of a dispute). Do not repeat queries already run. Set stop=true when more searching is unlikely to
change the answer.

Question: {question}
Sub-questions:
{subqs}
Queries already run:
{queries}
Evidence so far (id, domain, tier, date, claim):
{ledger}

Reply with ONLY this JSON:
{{"coverage": [{{"subq": "Q1", "status": "answered|thin|conflicting|missing", "note": "..."}}],
  "followups": [{{"subq": "Q2", "query": "...", "why": "..."}}],
  "stop": true|false, "stop_reason": "..."}}"""

WRITER = """You write the answer to a research question using ONLY the evidence provided. Today is {today}.
""" + DATA_NOT_INSTRUCTIONS + """

Rules:
1. Write 3 to 8 sentences. The first sentence answers the question directly (bottom line first).
2. Every sentence of kind "fact" cites one or more evidence ids, and the cited quotes must state what the
   sentence says, including every number, date, name and qualifier. Do not add facts from memory.
3. A sentence that draws a conclusion the quotes do not state outright is kind "inference" and must cite the
   evidence it reasons from. Keep inferences few and modest.
4. When information is time-sensitive, say "as of <date>" using the evidence dates.
5. Where sources disagree, do not pick a side silently: describe the disagreement in "conflicts".
6. List what the evidence could not establish in "gaps".
7. If the evidence does not answer the core question, set status "insufficient", write one or two sentences
   saying what IS established (with cites) and what is missing. Never fill the gap from memory.
8. Prefer primary and reputable sources, and newer evidence over older on time-sensitive points.

Question: {question}

<evidence_ledger>
{ledger}
</evidence_ledger>

Reply with ONLY this JSON:
{{"status": "answered|partial|insufficient",
  "answer": [{{"text": "sentence without citation markers", "cites": ["E1"], "kind": "fact|inference"}}],
  "conflicts": [{{"point": "...", "positions": [{{"claim": "...", "cites": ["E2"]}}, {{"claim": "...", "cites": ["E5"]}}],
                 "why": "likely reason they differ (date, definition, method), or unknown"}}],
  "gaps": ["..."],
  "confidence": "high|medium|low", "confidence_reason": "..."}}"""

CHECKER = """You are an independent fact-checker. For each numbered sentence, decide whether the quoted evidence
it cites supports it. You see only the sentence and its quotes; judge only from the quotes.
""" + DATA_NOT_INSTRUCTIONS + """

Labels:
- "supported": every factual element (who, what, when, how much, qualifiers like "only", "first", "most")
  is stated in the quotes. For kind=inference: the conclusion follows reasonably from the quotes.
- "partial": the core is supported but a detail is missing from the quotes, overstated, or more certain than
  the quotes allow.
- "unsupported": the quotes do not state it, are off-topic, or contradict it.
Be strict: a plausible sentence that the quotes do not actually state is not supported.

{items}

Reply with ONLY this JSON:
{{"results": [{{"i": 0, "label": "supported|partial|unsupported", "reason": "under 20 words"}}]}}"""

REPAIR = """Some sentences in a research answer failed an independent fact-check. Fix them using ONLY the evidence.
""" + DATA_NOT_INSTRUCTIONS + """

For each flagged sentence choose:
- "rewrite": narrow the sentence to exactly what the cited quotes state (you may cite different evidence ids),
- "drop": if no evidence supports any useful version of it.
Do not introduce facts that are not in the evidence.

Question: {question}

Flagged sentences:
{flagged}

<evidence_ledger>
{ledger}
</evidence_ledger>

Reply with ONLY this JSON:
{{"revisions": [{{"i": 2, "action": "rewrite|drop", "text": "...", "cites": ["E4"], "kind": "fact|inference"}}]}}"""
