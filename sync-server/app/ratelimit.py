"""Jednoduchý limit požadavků v paměti (posuvné okno na klíč). Server běží v jednom procesu."""
from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, max_keys: int = 50_000):
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def check(self, key: str, limit: int, window_s: int, now: float | None = None) -> int:
        """Započítá požadavek. Vrací 0, nebo počet sekund do uvolnění limitu."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if len(self._hits) > self._max_keys:
                self._prune(now)
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= window_s:
                hits.popleft()
            if len(hits) >= limit:
                return max(1, int(window_s - (now - hits[0])) + 1)
            hits.append(now)
            return 0

    def _prune(self, now: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] > 3600]:
            del self._hits[key]
        if len(self._hits) > self._max_keys:      # krajní případ: zahodit vše, raději než růst bez omezení
            self._hits.clear()

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
