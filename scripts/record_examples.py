"""Record live runs of the example questions so the UI can replay them instantly (no credits) for judges.

Usage: python scripts/record_examples.py [id ...]     (refuses to run in mock mode)
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from receipts.agent import research  # noqa: E402
from receipts.config import get_settings  # noqa: E402
from receipts.mock import build_clients  # noqa: E402


async def main(ids: list[str]):
    s = get_settings()
    if s.mock or not s.live_ready:
        sys.exit("Needs live keys and RC_MOCK unset: recordings must be real runs.")
    s.use_cache = False  # record a genuinely fresh run
    llm, tavily = build_clients(s)
    examples = json.loads((ROOT / "examples" / "examples.json").read_text())
    out_dir = ROOT / "examples" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    for ex in examples:
        if ids and ex["id"] not in ids:
            continue
        print(f"recording {ex['id']}: {ex['question']}", flush=True)
        t0, events, final = time.perf_counter(), [], None
        async for ev in research(ex["question"], llm, tavily, s):
            ev["t"] = int((time.perf_counter() - t0) * 1000)
            events.append(ev)
            if ev["type"] == "final":
                final = ev["report"]
        if not final:
            print("  no final report; not saved", flush=True)
            continue
        (out_dir / f"{ex['id']}.json").write_text(json.dumps({
            "question": ex["question"], "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "events": events}, indent=1))
        st = final["stats"]
        print(f"  {final['status']} · {st['sentences_supported_final']}/{st['sentences_final']} supported · "
              f"{st['quotes_verified']} verified / {st['quotes_rejected']} rejected · ${st['cost_usd']:.4f} · {st['elapsed_s']}s", flush=True)
    await llm.aclose()
    await tavily.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
