# Submission form answers (https://hackathon.odsc.ai/submit)

- **Team name:** Receipts
- **Team captain:** Mridhula Srinivasan
- **Captain email:** (yours)
- **What did you build?**
  Receipts is a research agent where every sentence of the answer shows its receipt: a verbatim quote from a live web page, the site, date and source tier, and a link that opens the page scrolled to that quote. Open models on Nebius Token Factory plan sub-questions (each with a counter-evidence query), read pages found by Tavily, and extract quotes. A program checks that each quote is actually on the page and discards any that aren't. A coverage audit decides whether to search again or stop. The answer is written sentence by sentence, then a different model fact-checks each sentence against only its cited quotes, with a rule that every figure must appear in them. Failing sentences are rewritten or removed, and the removals are shown. Disagreements between sources and gaps are listed, and when the web can't support an answer it says "insufficient evidence" instead of guessing. On our 12-question benchmark, [[X]]% of Receipts' sentences were supported by what they cite, vs [[Y]]% for the template's one-shot agent, scored by the same independent checker.
- **Public repository URL:** https://github.com/mritopper/receipts
- **Demo video URL:** (YouTube unlisted link)
- **Live app URL:** (Render URL, optional)
- **Write-up or report URL:** https://github.com/mritopper/receipts/blob/main/eval/RESULTS.md
- Tick the confirmation box.
