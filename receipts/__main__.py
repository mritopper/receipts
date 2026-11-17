"""CLI: python -m receipts "your question"  (live trace on stderr, sourced answer on stdout)."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .agent import research, to_markdown
from .config import get_settings
from .mock import build_clients

ICON = {"supported": "✓", "partial": "~", "unsupported": "✗", "unchecked": "?"}


def _trace(ev: dict) -> str | None:
    t = ev["type"]
    if t == "start":
        mode = "MOCK MODE (offline, not real results)" if ev["mock"] else "live"
        return f"Receipts · {mode} · models: " + ", ".join(f"{k}={v}" for k, v in ev["models"].items())
    if t == "plan":
        return "plan: " + " | ".join(f"{q['id']} {q['text']}" for q in ev["subquestions"]) + (
            f"  (fresh: {ev['time_range']})" if ev["needs_fresh"] else "")
    if t == "search":
        extra = " (broadened)" if ev.get("broadened") else ""
        return f"  search [{ev['kind']}] {ev['query']!r} -> {ev['status']}, {ev['n_results']} results, {len(ev['new_sources'])} new{extra}"
    if t == "read":
        rej = f", {len(ev['rejected'])} rejected" if ev["rejected"] else ""
        sus = f", injection-like text: {','.join(ev['suspicious'])}" if ev.get("suspicious") else ""
        return f"  read {ev['source_id']} {ev['domain']}: {len(ev['accepted'])} verified quotes{rej}{sus}{(' ' + ev['error']) if ev.get('error') else ''}"
    if t == "coverage":
        cov = ", ".join(f"{c['subq']}:{c['status']}" for c in ev["deterministic"])
        nxt = "; follow-ups: " + " | ".join(f["query"] for f in ev["followups"]) if not ev["stop"] else f"; stop: {ev['stop_reason']}"
        return f"coverage r{ev['round']}: {cov}{nxt}"
    if t == "check":
        return "check: " + " ".join(ICON.get(s.get("label"), "?") for s in ev["answer"])
    if t == "repair":
        return f"repair: {len(ev['revisions'])} revisions, {len(ev['removed'])} removed"
    if t in ("warning", "error"):
        return f"{t.upper()}: {ev['message']}"
    return None


async def main(question: str, as_json: bool, save: str | None) -> int:
    s = get_settings()
    if not s.mock and not s.live_ready:
        print("Missing NEBIUS_API_KEY or TAVILY_API_KEY. Put them in .env (see .env.example), "
              "or set RC_MOCK=1 for an offline dry run.", file=sys.stderr)
        return 2
    llm, tavily = build_clients(s)
    report = None
    try:
        async for ev in research(question, llm, tavily, s):
            line = _trace(ev)
            if line:
                print(line, file=sys.stderr, flush=True)
            if ev["type"] == "final":
                report = ev["report"]
            if ev["type"] == "error":
                return 1
    finally:
        await llm.aclose()
        await tavily.aclose()
    if report is None:
        return 1
    st = report["stats"]
    print(f"\n{st['quotes_verified']} quotes verified, {st['quotes_rejected']} rejected · "
          f"{st['sentences_supported_final']}/{st['sentences_final']} sentences supported · "
          f"{st['llm_calls']} model calls, {st['tavily_calls']} searches · ${st['cost_usd']:.4f} · {st['elapsed_s']}s\n",
          file=sys.stderr)
    print(json.dumps(report, indent=2) if as_json else to_markdown(report))
    if save:
        Path(save).write_text(json.dumps(report, indent=2))
    return 0


def cli() -> None:
    ap = argparse.ArgumentParser(prog="receipts", description="Research a question; every sentence shows its source.")
    ap.add_argument("question", nargs="*")
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    ap.add_argument("--save", help="also write the JSON report to this path")
    a = ap.parse_args()
    q = " ".join(a.question).strip() or "What changed in the EU AI Act timeline in 2025 and 2026?"
    sys.exit(asyncio.run(main(q, a.json, a.save)))


if __name__ == "__main__":
    cli()
