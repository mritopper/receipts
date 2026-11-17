"""Tavily search: live web results with full page text, canonical-URL + near-duplicate dedupe, source tiers."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse, urlunparse

import httpx

from .config import Settings

# Heuristic source tiers. A hint shown to the writer, the verifier and the reader, not ground truth.
_PRIMARY_SUFFIXES = (".gov", ".edu", ".mil", ".int", ".gov.uk", ".ac.uk", ".europa.eu", ".gc.ca", ".gov.au")
_PRIMARY_DOMAINS = {
    "nih.gov", "who.int", "cdc.gov", "fda.gov", "ftc.gov", "nasa.gov", "noaa.gov", "nature.com", "science.org",
    "thelancet.com", "nejm.org", "bmj.com", "jamanetwork.com", "cochranelibrary.com", "arxiv.org", "sec.gov",
    "un.org", "worldbank.org", "imf.org", "oecd.org", "federalreserve.gov", "bls.gov", "census.gov", "iea.org",
    "europarl.europa.eu", "eur-lex.europa.eu", "github.com", "python.org", "w3.org", "ietf.org",
}
_REPUTABLE_DOMAINS = {
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "nytimes.com", "washingtonpost.com", "wsj.com",
    "theguardian.com", "britannica.com", "wikipedia.org", "scientificamerican.com", "nationalgeographic.com",
    "mayoclinic.org", "clevelandclinic.org", "economist.com", "ft.com", "bloomberg.com", "cnbc.com",
    "techcrunch.com", "theverge.com", "arstechnica.com", "wired.com", "npr.org", "pbs.org", "axios.com",
    "politico.com", "statnews.com", "nature.com", "technologyreview.com", "spectrum.ieee.org", "pewresearch.org",
    "brookings.edu", "cfr.org", "factcheck.org", "politifact.com", "snopes.com", "fullfact.org",
}

_TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_src|cmpid|ocid)", re.I)


def canonical_url(url: str) -> str:
    """Normalise a URL so the same page reached two ways is stored once."""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    query = "&".join(q for q in p.query.split("&") if q and not _TRACKING.match(q.split("=")[0]))
    path = re.sub(r"/+$", "", p.path) or "/"
    return urlunparse(("https", host, path, "", query, ""))


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def source_tier(url: str) -> str:
    host = domain_of(url)
    if host in _PRIMARY_DOMAINS or host.endswith(_PRIMARY_SUFFIXES) or any(host.endswith("." + d) for d in _PRIMARY_DOMAINS):
        return "primary"
    if host in _REPUTABLE_DOMAINS or any(host.endswith("." + d) for d in _REPUTABLE_DOMAINS):
        return "reputable"
    return "unvetted"


def _fingerprint(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:120]
    return hashlib.md5(" ".join(words).encode()).hexdigest() if len(words) >= 40 else ""


@dataclass
class Source:
    id: str
    url: str
    title: str
    domain: str
    tier: str
    published: str | None
    snippet: str
    text: str  # page text the agent reads; never shown raw to a model outside <untrusted_source> tags
    score: float
    found_by: list[str] = field(default_factory=list)  # query ids that surfaced it
    suspicious: list[str] = field(default_factory=list)  # prompt-injection patterns spotted in the text

    def public(self) -> dict:
        d = asdict(self)
        d.pop("text")
        d["chars"] = len(self.text)
        return d


class SourcePool:
    """All sources seen in one research run, deduplicated by canonical URL and by content fingerprint."""

    def __init__(self, max_sources: int, source_chars: int):
        self.by_url: dict[str, Source] = {}
        self.fingerprints: dict[str, str] = {}
        self.max_sources = max_sources
        self.source_chars = source_chars
        self.dupes = 0

    def __len__(self) -> int:
        return len(self.by_url)

    def all(self) -> list[Source]:
        return list(self.by_url.values())

    def add(self, results: list[dict], query_id: str) -> list[Source]:
        from .safety import scan_injection  # local import keeps search importable on its own

        new: list[Source] = []
        for r in results:
            url = r.get("url") or ""
            if not url.startswith("http"):
                continue
            key = canonical_url(url)
            if key in self.by_url:
                if query_id not in self.by_url[key].found_by:
                    self.by_url[key].found_by.append(query_id)
                self.dupes += 1
                continue
            text = (r.get("raw_content") or "").strip() or (r.get("content") or "").strip()
            fp = _fingerprint(text)
            if fp and fp in self.fingerprints:  # syndicated copy of a page we already have
                self.by_url[self.fingerprints[fp]].found_by.append(query_id)
                self.dupes += 1
                continue
            if len(self.by_url) >= self.max_sources:
                break
            src = Source(
                id=f"S{len(self.by_url) + 1}",
                url=url,
                title=(r.get("title") or url)[:160],
                domain=domain_of(url),
                tier=source_tier(url),
                published=(r.get("published_date") or None),
                snippet=(r.get("content") or "").strip()[:500],
                text=text[: self.source_chars],
                score=float(r.get("score") or 0.0),
                found_by=[query_id],
                suspicious=scan_injection(text),
            )
            self.by_url[key] = src
            if fp:
                self.fingerprints[fp] = key
            new.append(src)
        return new


class SearchError(RuntimeError):
    pass


class Tavily:
    URL = "https://api.tavily.com/search"

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.s = settings
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0))

    async def search(
        self,
        query: str,
        *,
        depth: str = "advanced",
        max_results: int = 5,
        topic: str = "general",
        time_range: str | None = None,
    ) -> tuple[list[dict], int, bool]:
        """Returns (results, credits used, served_from_cache). Raises SearchError after retries."""
        payload = {
            "query": query[:380],
            "search_depth": depth,
            "max_results": max_results,
            "topic": topic,
            "include_answer": False,  # the model writes the answer from page text, not Tavily's summary
            "include_raw_content": "text",
            "include_usage": True,
        }
        if time_range:
            payload["time_range"] = time_range
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        path = self.s.cache_dir / "tavily" / f"{key}.json"
        if self.s.use_cache and path.exists():
            try:
                return json.loads(path.read_text())["results"], 0, True
            except Exception:  # noqa: BLE001
                pass
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = await self.http.post(
                    self.URL, headers={"Authorization": f"Bearer {self.s.tavily_api_key}"}, json=payload
                )
                if r.status_code in (429, 432, 433, 500, 502, 503, 504):
                    last = SearchError(f"Tavily HTTP {r.status_code}")
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    raise SearchError(f"Tavily HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
                results = data.get("results", []) or []
                credits = int((data.get("usage") or {}).get("credits") or (2 if depth == "advanced" else 1))
                if self.s.use_cache and results:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"results": results}))
                return results, credits, False
            except httpx.HTTPError as e:
                last = e
                await asyncio.sleep(1.0 * (attempt + 1))
        raise SearchError(f"Tavily search failed after retries: {last}")

    async def aclose(self) -> None:
        await self.http.aclose()
