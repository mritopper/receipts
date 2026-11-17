"""Live smoke test: are both keys valid, which models were picked, does one call each way work?"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from receipts.config import get_settings  # noqa: E402
from receipts.llm import Ledger  # noqa: E402
from receipts.search import Tavily  # noqa: E402
from receipts.llm import TokenFactory  # noqa: E402


async def main():
    s = get_settings()
    s.use_cache = False
    print("NEBIUS_API_KEY:", "set" if s.nebius_api_key else "MISSING", "· TAVILY_API_KEY:", "set" if s.tavily_api_key else "MISSING")
    if not s.live_ready:
        sys.exit(2)
    tf, tv = TokenFactory(s), Tavily(s)
    models = await tf.discover()
    print("discovery:", tf.discovery_note or "ok", "·", len(tf.model_base), "models listed")
    for k, v in models.items():
        print(f"  {k:6} -> {v}  price/M in,out = {tuple(round(p * 1e6, 3) for p in tf.price(k))}")
    led = Ledger()
    for tier in ("fast", "strong", "check"):
        try:
            data, rec = await tf.chat_json(tier, [{"role": "user", "content": 'Reply with ONLY {"ok": true}'}],
                                           role="smoke", ledger=led, max_tokens=200, use_cache=False)
            print(f"  {tier}: {data} · {rec.latency_ms} ms · {rec.prompt_tokens}+{rec.completion_tokens} tok")
        except Exception as e:  # noqa: BLE001
            print(f"  {tier}: FAILED {type(e).__name__}: {str(e)[:200]}")
    res, credits, _ = await tv.search("Nebius Token Factory", depth="basic", max_results=2)
    print(f"tavily: {len(res)} results, {credits} credits, raw_content chars: {[len(r.get('raw_content') or '') for r in res]}")
    await tf.aclose()
    await tv.aclose()


asyncio.run(main())
