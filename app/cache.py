"""Cache lifetime is process-wide; correctness never relies on a cache hit."""
import hashlib
import json
import threading
import time
from copy import deepcopy
from functools import lru_cache

from app.config import settings


class MemoryCache:
    def __init__(self):
        self._store = {}
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    def get(self, key):
        with self._lock:
            item = self._store.get(key)
            if item is not None and item[1] > time.monotonic():
                self.hits += 1
                return deepcopy(item[0])
            self._store.pop(key, None)
            self.misses += 1
            return None

    def set(self, key, value, ttl):
        with self._lock:
            if len(self._store) >= 1000:
                now = time.monotonic()
                self._store = {k: v for k, v in self._store.items() if v[1] > now}
                if len(self._store) >= 1000:
                    self._store.pop(next(iter(self._store)))
            self._store[key] = (deepcopy(value), time.monotonic() + ttl)

    def delete(self, key):
        with self._lock:
            self._store.pop(key, None)


class RedisCache:
    def __init__(self):
        import redis
        self.redis = redis
        self.client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True,
                                            socket_connect_timeout=2, socket_timeout=2)

    def get(self, key):
        try:
            raw = self.client.get(key)
            return json.loads(raw) if raw is not None else None
        except (self.redis.RedisError, ValueError):
            return None

    def set(self, key, value, ttl):
        try:
            self.client.set(key, json.dumps(value, ensure_ascii=False, allow_nan=False), ex=ttl)
        except self.redis.RedisError:
            pass

    def delete(self, key):
        try:
            self.client.delete(key)
        except self.redis.RedisError:
            pass


@lru_cache(maxsize=1)
def get_cache():
    return RedisCache() if settings.CACHE_BACKEND == "redis" else MemoryCache()


def fingerprint(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def request_fingerprint(project_id, input_text):
    return fingerprint({"project_id": project_id, "input_text": input_text.strip()})
