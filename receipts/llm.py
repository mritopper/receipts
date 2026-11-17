"""Nebius Token Factory client (OpenAI-compatible): model discovery, role routing, retries, cost accounting."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import FALLBACK_MODELS, FALLBACK_PRICES, MODEL_PATTERNS, TIERS, Settings


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------- parsing helpers

def strip_reasoning(text: str) -> str:
    """Hybrid-reasoning chat templates can pre-fill <think>, so a reply may contain only a closing tag."""
    if "</think>" in text:
        text = text.split("</think>")[-1]
    return text.replace("<think>", "").strip()


def extract_json(text: str) -> Any:
    """Pull the first complete JSON value out of a model reply (handles fences, prose, think blocks)."""
    text = strip_reasoning(text)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError("no JSON found in model output")
    start = min(starts)
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("unterminated JSON in model output")


# --------------------------------------------------------------------------- accounting

@dataclass
class CallRecord:
    n: int
    role: str
    tier: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    latency_ms: int
    cost_usd: float
    cached: bool = False
    stage: str = ""
    ok: bool = True
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Ledger:
    records: list[CallRecord] = field(default_factory=list)
    tavily_credits: int = 0
    tavily_calls: int = 0
    _n: int = 0

    def next_n(self) -> int:
        self._n += 1
        return self._n

    @property
    def cost(self) -> float:
        return sum(r.cost_usd for r in self.records)

    @property
    def tokens(self) -> int:
        return sum(r.prompt_tokens + r.completion_tokens for r in self.records)

    def summary(self) -> dict:
        return {
            "llm_calls": len(self.records),
            "tokens": self.tokens,
            "cost_usd": round(self.cost, 5),
            "tavily_calls": self.tavily_calls,
            "tavily_credits": self.tavily_credits,
            "by_tier": self.by_tier(),
        }

    def by_tier(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.records:
            t = out.setdefault(r.tier, {"calls": 0, "tokens": 0, "cost_usd": 0.0})
            t["calls"] += 1
            t["tokens"] += r.prompt_tokens + r.completion_tokens
            t["cost_usd"] += r.cost_usd
        return out


# --------------------------------------------------------------------------- client

class TokenFactory:
    """Thin async wrapper over the Token Factory chat-completions API."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.s = settings
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(150.0, connect=15.0))
        self.models: dict[str, str] = {}
        self.model_base: dict[str, str] = {}  # model id -> base url that serves it
        self.prices: dict[str, tuple[float, float]] = {}  # model id -> (in, out) USD per token
        self.supports_template_kwargs = True
        self._sem = asyncio.Semaphore(settings.max_parallel_llm)
        self._discovered = False
        self.discovery_note = ""

    # ---- discovery ------------------------------------------------------------------
    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.s.nebius_api_key}", "Content-Type": "application/json"}

    async def discover(self) -> dict[str, str]:
        """Ask Token Factory which open models it serves and what they cost. Falls back to known IDs."""
        if self._discovered:
            return self.models
        listed: dict[str, dict] = {}
        for base in self.s.base_urls:
            try:
                r = await self.http.get(f"{base}/models", params={"verbose": "true"}, headers=self._headers)
                r.raise_for_status()
                for m in r.json().get("data", []):
                    listed.setdefault(m["id"], m)
                    self.model_base.setdefault(m["id"], base)
            except Exception as e:  # noqa: BLE001 - discovery is best-effort
                self.discovery_note += f"{base}: {type(e).__name__}; "
        for tier in TIERS:
            override = self.s.model_overrides.get(tier)
            chosen = override
            if not chosen:
                for pat in MODEL_PATTERNS[tier]:
                    hit = next((i for i in listed if re.search(pat, i, re.I)), None)
                    # The verifier should be a different model from the writer when the catalog allows it.
                    if hit and tier == "check" and hit == self.models.get("strong"):
                        continue
                    if hit:
                        chosen = hit
                        break
            self.models[tier] = chosen or FALLBACK_MODELS[tier]
            m = listed.get(self.models[tier])
            if m and isinstance(m.get("pricing"), dict):
                try:
                    self.prices[self.models[tier]] = (
                        float(m["pricing"]["prompt"]),
                        float(m["pricing"]["completion"]),
                    )
                except (KeyError, TypeError, ValueError):
                    pass
        self._discovered = True
        return self.models

    def price(self, tier: str) -> tuple[float, float]:
        model = self.models.get(tier, "")
        if model in self.prices:
            return self.prices[model]
        i, o = FALLBACK_PRICES[tier]
        return i / 1e6, o / 1e6

    # ---- cache ----------------------------------------------------------------------
    def _cache_path(self, key: str) -> Path:
        return self.s.cache_dir / "llm" / f"{key}.json"

    def _cache_get(self, key: str) -> dict | None:
        if not self.s.use_cache:
            return None
        p = self._cache_path(key)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:  # noqa: BLE001
                return None
        return None

    def _cache_put(self, key: str, value: dict) -> None:
        if not self.s.use_cache:
            return
        p = self._cache_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value))

    # ---- chat -----------------------------------------------------------------------
    async def chat(
        self,
        tier: str,
        messages: list[dict],
        *,
        role: str,
        ledger: Ledger,
        stage: str = "",
        thinking: bool = False,
        max_tokens: int = 900,
        temperature: float = 0.2,
        use_cache: bool = True,
    ) -> tuple[str, CallRecord]:
        await self.discover()
        model = self.models[tier]
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # Hybrid-reasoning families (Qwen3, Nemotron) take a thinking switch through the chat template;
        # gpt-oss takes a reasoning effort. Every role here wants short structured output, so both stay low.
        if self.supports_template_kwargs and re.search(r"qwen3|nemotron", model, re.I):
            body["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}
        if self.supports_template_kwargs and re.search(r"gpt-oss", model, re.I):
            body["reasoning_effort"] = "medium" if thinking else "low"

        key = hashlib.sha256(json.dumps([model, messages, max_tokens, temperature, thinking], sort_keys=True).encode()).hexdigest()
        cached = self._cache_get(key) if use_cache else None
        if cached:
            text, usage, latency_ms = cached["text"], cached["usage"], cached["latency_ms"]
        else:
            text, usage, latency_ms = await self._post(tier, body)
            if use_cache:
                self._cache_put(key, {"text": text, "usage": usage, "latency_ms": latency_ms})

        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        rt = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        pi, po = self.price(tier)
        rec = CallRecord(
            n=ledger.next_n(), role=role, tier=tier, model=self.models[tier], prompt_tokens=pt,
            completion_tokens=ct, reasoning_tokens=rt, latency_ms=latency_ms,
            cost_usd=pt * pi + ct * po, cached=bool(cached), stage=stage,
        )
        ledger.records.append(rec)
        return text, rec

    async def chat_json(self, tier: str, messages: list[dict], **kw) -> tuple[Any, CallRecord]:
        """Chat and parse JSON; on a malformed reply retry once with a sterner reminder and more room."""
        text, rec = await self.chat(tier, messages, **kw)
        try:
            return extract_json(text), rec
        except (ValueError, json.JSONDecodeError):
            retry = list(messages) + [
                {"role": "assistant", "content": strip_reasoning(text)[:1500]},
                {"role": "user", "content": "That was not valid JSON. Reply again with ONLY the JSON object, nothing else."},
            ]
            # If the model spent the whole budget "thinking", give it far more room on the second try.
            kw = dict(kw, max_tokens=max(int(kw.get("max_tokens", 900) * 3), 3000), use_cache=False)
            text, rec = await self.chat(tier, retry, **kw)
            rec.note = "json-retry"
            try:
                return extract_json(text), rec
            except (ValueError, json.JSONDecodeError) as e:
                rec.ok = False
                raise LLMError(f"{tier} model returned unparseable JSON for {kw.get('role')}") from e

    async def _post(self, tier: str, body: dict) -> tuple[str, dict, int]:
        model = body["model"]
        bases = [self.model_base.get(model)] if self.model_base.get(model) else []
        bases += [b for b in self.s.base_urls if b not in bases]
        last: Exception | None = None
        for attempt in range(4):
            base = bases[min(attempt // 2, len(bases) - 1)]
            t0 = time.perf_counter()
            try:
                async with self._sem:
                    r = await self.http.post(f"{base}/chat/completions", headers=self._headers, json=body)
                optional = [k for k in ("chat_template_kwargs", "reasoning_effort") if k in body]
                if r.status_code in (400, 422) and optional:
                    # This deployment does not accept the reasoning switch; drop it for good and retry.
                    self.supports_template_kwargs = False
                    body = {k: v for k, v in body.items() if k not in optional}
                    continue
                if r.status_code == 404 and len(bases) > 1:
                    continue
                if r.status_code in (429, 500, 502, 503, 504):
                    last = LLMError(f"HTTP {r.status_code}")
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    raise LLMError(f"Token Factory HTTP {r.status_code}: {r.text[:300]}")
                data = r.json()
                self.model_base[model] = base
                msg = data["choices"][0]["message"]
                text = msg.get("content") or ""
                if not text.strip() and msg.get("reasoning_content"):
                    text = msg["reasoning_content"]  # truncated mid-thought; chat_json will retry
                return text, data.get("usage") or {}, int((time.perf_counter() - t0) * 1000)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = e
                await asyncio.sleep(1.0 * (attempt + 1))
        raise LLMError(f"Token Factory unreachable for {model}: {last}")

    async def aclose(self) -> None:
        await self.http.aclose()
