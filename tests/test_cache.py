"""Tests for mb_crawler.cache."""

from __future__ import annotations

import json
import time
from pathlib import Path

from mb_crawler.cache import ResponseCache


class TestResponseCache:
    def test_put_and_get(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com/page1", "<html>ok</html>", 200)
        result = cache.get("https://example.com/page1")
        assert result is not None
        body, status = result
        assert body == "<html>ok</html>"
        assert status == 200

    def test_get_miss(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        assert cache.get("https://nonexistent.com") is None

    def test_disabled_cache_returns_none(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=False)
        cache.put("https://example.com", "body", 200)
        assert cache.get("https://example.com") is None

    def test_disabled_cache_put_is_noop(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=False)
        cache.put("https://example.com", "body", 200)
        assert not list(tmp_path.glob("*.json"))

    def test_expired_entry_returns_none(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, ttl=0, enabled=True)
        cache.put("https://example.com", "body", 200)
        time.sleep(0.01)
        assert cache.get("https://example.com") is None

    def test_invalidate_single_url(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://a.com", "a", 200)
        cache.put("https://b.com", "b", 200)
        cache.invalidate("https://a.com")
        assert cache.get("https://a.com") is None
        assert cache.get("https://b.com") is not None

    def test_invalidate_all(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://a.com", "a", 200)
        cache.put("https://b.com", "b", 200)
        cache.invalidate()
        assert cache.get("https://a.com") is None
        assert cache.get("https://b.com") is None

    def test_key_deterministic(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        assert cache._key("https://example.com") == cache._key("https://example.com")

    def test_different_urls_different_keys(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        assert cache._key("https://a.com") != cache._key("https://b.com")

    def test_creates_cache_dir(self, tmp_path: Path):
        cache_dir = tmp_path / "subdir" / "cache"
        cache = ResponseCache(cache_dir=cache_dir, enabled=True)
        cache.put("https://example.com", "body", 200)
        assert cache_dir.exists()

    def test_corrupt_json_returns_none(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        key = cache._key("https://example.com")
        (tmp_path / f"{key}.json").write_text("not valid json {{{")
        assert cache.get("https://example.com") is None

    def test_unicode_body(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        body = "<html>中文内容</html>"
        cache.put("https://example.com", body, 200)
        result = cache.get("https://example.com")
        assert result[0] == body
