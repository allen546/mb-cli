"""Disk-based HTTP response cache with TTL for tahuti."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from .config import config_dir

DEFAULT_CACHE_DIR = config_dir() / "cache"
DEFAULT_TTL = 900  # 15 minutes


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

    def get(self, url: str, allow_stale: bool = False) -> tuple[str, int] | None:
        """Return ``(body, status)`` if cached and fresh, else ``None``.
        
        If ``allow_stale`` is True, returns the cached value even if expired or invalidated.
        """
        if not self.enabled:
            return None
        p = self._path(url)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        
        is_expired = time.time() - data.get("ts", 0) > self.ttl
        is_invalidated = bool(data.get("invalidated", False))

        if (is_expired or is_invalidated) and not allow_stale:
            return None
        return data["body"], data["status"]

    def put(self, url: str, body: str, status: int) -> None:
        """Write a response to the cache."""
        if not self.enabled:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.cache_dir, 0o700)
        # Harden the parents too — mkdir(parents=True) would otherwise leave
        # ~/.config/tahuti and its cache/ at the umask default (0755),
        # making the credential-bearing tree traversable by other local users.
        for parent in (self.cache_dir, *self.cache_dir.parents):
            try:
                if parent.is_dir():
                    os.chmod(parent, 0o700)
            except OSError:
                pass
        data = {
            "url": url,
            "body": body,
            "status": status,
            "ts": time.time()
        }
        p = self._path(url)
        # Create 0600 from birth via a temp file, then atomically replace —
        # avoids any window where cached grade pages/JWTs are world-readable.
        fd, tmp = tempfile.mkstemp(
            dir=str(self.cache_dir), prefix=".cache_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def clear(self) -> int:
        """Delete every cache entry. Returns the number of files removed.

        Cached bodies include full grade pages and the MNN-hub Bearer JWT, so
        ``mb logout`` calls this to avoid leaving credentials on disk.
        """
        removed = 0
        if not self.cache_dir.exists():
            return 0
        for f in self.cache_dir.glob("*.json"):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def invalidate(self, url: str | None = None) -> None:
        """Mark one entry as invalidated (setting ts=0 and invalidated=True), or mark all entries if *url* is ``None``."""
        if url:
            p = self._path(url)
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    data["ts"] = 0
                    data["invalidated"] = True
                    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    try:
                        p.unlink()
                    except OSError:
                        pass
        elif self.cache_dir.exists():
            for f in self.cache_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    data["ts"] = 0
                    data["invalidated"] = True
                    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    try:
                        f.unlink()
                    except OSError:
                        pass
