"""Disk-based HTTP response cache with TTL for mb-crawler."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

DEFAULT_CACHE_DIR = Path.home() / ".config" / "mb-crawler" / "cache"
DEFAULT_TTL = 1800  # 30 minutes


class ResponseCache:
    """Simple disk-based cache keyed by URL hash.

    Each entry stores ``{url, status, body, timestamp}`` as a JSON file.
    """

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        ttl: int = DEFAULT_TTL,
        enabled: bool = True,
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self.ttl = ttl
        self.enabled = enabled

    def _key(self, url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:32]

    def _path(self, url: str) -> Path:
        return self.cache_dir / f"{self._key(url)}.json"

    def get(self, url: str) -> tuple[str, int] | None:
        """Return ``(body, status)`` if cached and fresh, else ``None``."""
        if not self.enabled:
            return None
        p = self._path(url)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if time.time() - data.get("ts", 0) > self.ttl:
            return None
        return data["body"], data["status"]

    def put(self, url: str, body: str, status: int) -> None:
        """Write a response to the cache."""
        if not self.enabled:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.cache_dir, 0o700)
        data = {"url": url, "body": body, "status": status, "ts": time.time()}
        p = self._path(url)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.chmod(p, 0o600)

    def invalidate(self, url: str | None = None) -> None:
        """Remove one entry, or flush the entire cache if *url* is ``None``."""
        if url:
            p = self._path(url)
            if p.exists():
                p.unlink()
        elif self.cache_dir.exists():
            for f in self.cache_dir.glob("*.json"):
                f.unlink()
