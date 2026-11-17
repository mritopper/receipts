"""FastAPI app: serves the UI and streams a research run as Server-Sent Events."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import research, to_markdown
from .config import ROOT, get_settings
from .guard import Guard
from .mock import build_clients

STATIC = Path(__file__).parent / "static"
EXAMPLES_FILE = ROOT / "examples" / "examples.json"
RUNS_DIR = ROOT / "examples" / "runs"


class AskBody(BaseModel):
    question: str = Field("", max_length=600)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


async def _with_heartbeat(agen, interval: float = 8.0):
    """Yield events from `agen`; yield None every `interval`s of silence so proxies keep the stream open."""
    it = agen.__aiter__()
    nxt = asyncio.ensure_future(it.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({nxt}, timeout=interval)
            if not done:
                yield None
                continue
            try:
                item = nxt.result()
            except StopAsyncIteration:
                return
            yield item
            nxt = asyncio.ensure_future(it.__anext__())
    finally:
        nxt.cancel()


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    app.state.settings = s
    app.state.llm, app.state.search = build_clients(s)
    app.state.guard = Guard(s)
    yield
    await app.state.llm.aclose()
    await app.state.search.aclose()


app = FastAPI(title="Receipts", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health(request: Request):
    s, g = request.app.state.settings, request.app.state.guard
    models = await request.app.state.llm.discover() if (s.live_ready or s.mock) else {}
    return {"ok": True, "mode": "mock" if s.mock else ("live" if s.live_ready else "needs_keys"), "models": models,
            "budget_left_usd": round(max(0.0, s.daily_budget_usd - g.spent), 3), "rate_limit_per_hour": s.rate_limit_per_hour}


@app.get("/api/examples")
async def examples():
    items = json.loads(EXAMPLES_FILE.read_text()) if EXAMPLES_FILE.exists() else []
    for it in items:
        it["recorded"] = (RUNS_DIR / f"{it['id']}.json").exists()
    return items


@app.post("/api/ask")
async def ask(body: AskBody, request: Request):
    s, g = request.app.state.settings, request.app.state.guard

    async def stream():
        if not (s.live_ready or s.mock):
            yield _sse({"type": "error", "message": "This server has no NEBIUS_API_KEY / TAVILY_API_KEY. Replay a recorded run instead."})
            return
        ok, why = g.allow(_ip(request))
        if not ok:
            yield _sse({"type": "error", "message": why, "limited": True})
            return
        spent = 0.0
        try:
            async for ev in _with_heartbeat(research(body.question, request.app.state.llm, request.app.state.search, s)):
                if ev is None:
                    yield ": ping\n\n"
                    continue
                if ev["type"] == "meter":
                    spent = ev["cost_usd"]
                yield _sse(ev)
        except Exception as e:  # noqa: BLE001 - surface, never hang the UI
            yield _sse({"type": "error", "message": f"Unexpected failure: {type(e).__name__}: {str(e)[:200]}"})
        finally:
            g.record(spent)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/replay/{run_id}")
async def replay(run_id: str, speed: float = 1.0):
    path = RUNS_DIR / f"{run_id}.json"
    if not run_id.replace("-", "").replace("_", "").isalnum() or not path.exists():
        return JSONResponse({"error": "no recording"}, status_code=404)
    run = json.loads(path.read_text())

    async def stream():
        prev = 0
        for ev in run["events"]:
            gap = max(0, ev.get("t", 0) - prev) / 1000
            prev = ev.get("t", 0)
            await asyncio.sleep(min(0.9, gap * 0.5) * max(0.0, min(speed, 1.0)))
            yield _sse({**ev, "replay": True, "recorded_at": run.get("recorded_at")})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/markdown", response_class=PlainTextResponse)
async def markdown(report: dict):
    return to_markdown(report)


def main() -> None:
    import os

    import uvicorn

    uvicorn.run("receipts.server:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
