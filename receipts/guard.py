"""Guard rails for a public demo that spends the owner's credits: per-IP rate limit and a daily budget."""
from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from datetime import date
from pathlib import Path

from .config import Settings


class Guard:
    def __init__(self, settings: Settings):
        self.s = settings
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self._file = settings.cache_dir / "budget.json"
        self._day = date.today().isoformat()
        self._spent = 0.0
        try:
            d = json.loads(self._file.read_text())
            if d.get("day") == self._day:
                self._spent = float(d.get("spent", 0.0))
        except Exception:  # noqa: BLE001
            pass

    def allow(self, ip: str) -> tuple[bool, str]:
        today = date.today().isoformat()
        if today != self._day:
            self._day, self._spent = today, 0.0
        if self._spent >= self.s.daily_budget_usd:
            return False, "The demo has used today's credit budget. Replay a recorded run, or run it yourself from the repo."
        now = time.time()
        q = self.hits[ip]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= self.s.rate_limit_per_hour:
            return False, f"Rate limit: {self.s.rate_limit_per_hour} live questions per hour per visitor. Recorded runs are unlimited."
        q.append(now)
        return True, ""

    def record(self, cost_usd: float) -> None:
        self._spent += cost_usd
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps({"day": self._day, "spent": self._spent}))
        except Exception:  # noqa: BLE001
            pass

    @property
    def spent(self) -> float:
        return self._spent
