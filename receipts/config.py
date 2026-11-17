"""Runtime configuration. Everything is overridable with environment variables or a local .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:  # an empty `KEY=` placeholder must not shadow a real exported variable
            os.environ.setdefault(key.strip(), value)


_load_dotenv(ROOT / ".env")

# Roles, not model names. The agent asks Token Factory which open models it serves and picks one per role.
#   fast   - planning, quote extraction (many small calls)
#   strong - gap analysis, synthesis, repair (few calls that need the best reasoning)
#   check  - the independent verifier; deliberately a different model family from `strong` when possible,
#            so the writer is not grading its own homework
TIERS = ("fast", "strong", "check")

DEFAULT_BASE_URLS = (
    "https://api.tokenfactory.nebius.com/v1",
    "https://api.tokenfactory.us-central1.nebius.com/v1",
    "https://api.studio.nebius.com/v1",
)

# Used only if /models discovery fails. The template's default model is the safest fallback.
FALLBACK_MODELS = {
    "fast": "meta-llama/Llama-3.3-70B-Instruct",
    "strong": "meta-llama/Llama-3.3-70B-Instruct",
    "check": "meta-llama/Llama-3.3-70B-Instruct",
}

# Regexes used to find a model per role in the live /models list (first match wins). Instruct models are
# preferred over long-thinking ones: every role returns structured JSON, and latency matters in a demo.
MODEL_PATTERNS = {
    "fast": [
        r"qwen3[.\d]*-30b-a3b-instruct", r"gpt-oss-20b", r"nemotron-3-nano-30b", r"llama-3\.3-70b-instruct",
    ],
    "strong": [
        r"qwen3[.\d]*-235b-a22b-instruct", r"deepseek-v3", r"kimi-k2", r"gpt-oss-120b", r"minimax-m3",
        r"llama-3\.3-70b-instruct",
    ],
    "check": [
        r"llama-3\.3-70b-instruct", r"gpt-oss-120b", r"nemotron-3-super", r"qwen3[.\d]*-30b-a3b-instruct",
    ],
}

# USD per 1M tokens (input, output), only used if the /models listing carries no pricing.
FALLBACK_PRICES = {"fast": (0.10, 0.30), "strong": (0.20, 0.60), "check": (0.13, 0.40)}


@dataclass
class Settings:
    nebius_api_key: str = field(default_factory=lambda: os.environ.get("NEBIUS_API_KEY", ""))
    tavily_api_key: str = field(default_factory=lambda: os.environ.get("TAVILY_API_KEY", ""))
    base_urls: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            u.strip().rstrip("/")
            for u in os.environ.get("NEBIUS_BASE_URL", ",".join(DEFAULT_BASE_URLS)).split(",")
            if u.strip()
        )
    )
    model_overrides: dict[str, str] = field(
        default_factory=lambda: {
            t: os.environ[f"RC_MODEL_{t.upper()}"] for t in TIERS if os.environ.get(f"RC_MODEL_{t.upper()}")
        }
    )
    mock: bool = field(default_factory=lambda: os.environ.get("RC_MOCK", "") == "1")
    cache_dir: Path = field(default_factory=lambda: Path(os.environ.get("RC_CACHE_DIR", ROOT / ".cache")))
    use_cache: bool = field(default_factory=lambda: os.environ.get("RC_CACHE", "1") == "1")
    max_parallel_llm: int = field(default_factory=lambda: int(os.environ.get("RC_MAX_PARALLEL", "8")))
    max_subquestions: int = field(default_factory=lambda: int(os.environ.get("RC_MAX_SUBQ", "4")))
    max_rounds: int = field(default_factory=lambda: int(os.environ.get("RC_MAX_ROUNDS", "2")))
    results_per_query: int = field(default_factory=lambda: int(os.environ.get("RC_RESULTS_PER_QUERY", "5")))
    max_sources: int = field(default_factory=lambda: int(os.environ.get("RC_MAX_SOURCES", "18")))
    source_chars: int = field(default_factory=lambda: int(os.environ.get("RC_SOURCE_CHARS", "9000")))
    # Public-demo guard rails (a hosted demo spends the owner's credits)
    rate_limit_per_hour: int = field(default_factory=lambda: int(os.environ.get("RC_RATE_PER_HOUR", "6")))
    daily_budget_usd: float = field(default_factory=lambda: float(os.environ.get("RC_DAILY_BUDGET_USD", "3.0")))

    @property
    def live_ready(self) -> bool:
        return bool(self.nebius_api_key and self.tavily_api_key)


def get_settings() -> Settings:
    return Settings()
