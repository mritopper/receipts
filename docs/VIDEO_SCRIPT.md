# Demo video script (target 2:30)

Record the web UI at 1440×900 in Chrome (Text Fragments highlight only works in Chromium browsers).
Use a **live** question for the first run so judges see real Token Factory + Tavily calls; recorded replays are
fine for the second example. Before recording: `python scripts/smoke.py`, close other tabs, browser zoom 110%.
Never show the `.env` file or a terminal with keys.

| Time | Screen | Say (roughly) |
|---|---|---|
| 0:00–0:15 | Landing page | "AI research tools cite sources, but when you click through, the source often doesn't say what the sentence claims. Receipts is a research agent where every sentence comes with a receipt you can check in one click." |
| 0:15–0:25 | Type the live question, press Research | "This is a live run: open models on Nebius Token Factory, live search through Tavily." |
| 0:25–0:55 | Trace panel filling | "It plans sub-questions, and for each one it also searches for the other side: criticism, corrections, newer numbers. It reads every page and copies verbatim quotes. Then a program, not a model, checks each quote is really on the page. These red ones weren't, so they're thrown away." (expand "quotes not found on the page") |
| 0:55–1:20 | Answer appears; click a citation | "Each sentence cites evidence. Click one and you get the receipt: the exact quote, the site, its tier, the date." Click **Open page at this quote**. "And this opens the real page, scrolled to that sentence and highlighted." |
| 1:20–1:40 | Scroll to *Where sources disagree* and *Caught by the fact-check* | "A second, different model fact-checks every sentence against only its quotes, and a rule checks every number appears in them. This sentence failed, so it was removed, and you can see that it was." |
| 1:40–1:55 | Click the 2027 Nobel example | "When the web can't support an answer, it says so: insufficient evidence, and here's everything it searched." |
| 1:55–2:15 | README benchmark table | "On 12 questions, scored by the same independent checker, the template's one-shot agent had X% of sentences supported by what they cite. Receipts had Y%, for about Z cents a question." |
| 2:15–2:30 | Copy as Markdown, paste into a doc | "And the answer exports with its quotes as footnotes, ready for a memo or an article. Receipts: answers you can check." |

Upload to YouTube as **Unlisted** (not Private), or Loom with link sharing on. Check it plays in a private window.
