"""Redis-backed shared security state for multi-instance MCP gateways."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import redis


class SharedState:
    def __init__(self, url: str):
        scheme = urlsplit(url).scheme
        if scheme != "rediss" and not (scheme == "redis" and os.getenv("ALLOW_INSECURE_REDIS") == "1"):
            raise ValueError("AUTH_REDIS_URL must use TLS (rediss://)")
        self.db = redis.Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5)
        self.db.ping()

    def claim_once(self, identifier: str) -> bool:
        return bool(self.db.set("devmcp:oauth:used:" + identifier, "1", ex=35 * 86400, nx=True))

    def check_login_budget(self, identifier: str, ceiling: int = 10) -> bool:
        """True if attempt is allowed. Atomic count across all gateway instances."""
        import hashlib
        key = "devmcp:oauth:login:" + hashlib.sha256(identifier.encode()).hexdigest()
        count = self.db.incr(key)
        if count == 1:
            self.db.expire(key, 300)
        return count <= ceiling
