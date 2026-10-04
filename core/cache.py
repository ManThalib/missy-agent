"""Simple on-disk TTL cache for provider responses and enrichment data."""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional


class DiskCache:
    """Disk-backed cache with per-key TTL.

    Parameters
    ----------
    directory:
        Where to store cache entries. Defaults to ``$MISSY_CACHE_DIR`` or
        ``/tmp/missy-cache``.
    default_ttl_seconds:
        Time-to-live for entries that do not specify one.
    """

    def __init__(
        self,
        directory: Optional[str] = None,
        default_ttl_seconds: float = 300.0,
    ) -> None:
        self.directory = Path(directory or os.environ.get("MISSY_CACHE_DIR") or "/tmp/missy-cache")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.default_ttl = default_ttl_seconds

    def _key(self, namespace: str, *parts: Any) -> str:
        payload = "|".join(str(p) for p in parts)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
        return f"{namespace}_{digest}.json"

    def get(self, namespace: str, *parts: Any) -> Optional[Any]:
        path = self.directory / self._key(namespace, *parts)
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            if entry.get("expires", 0) < time.time():
                path.unlink(missing_ok=True)
                return None
            return entry.get("value")
        except Exception:
            return None

    def set(
        self,
        namespace: str,
        *parts: Any,
        value: Any,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        path = self.directory / self._key(namespace, *parts)
        entry = {"expires": time.time() + ttl, "value": value}
        try:
            path.write_text(json.dumps(entry, default=str), encoding="utf-8")
        except Exception:
            pass

    def clear(self) -> None:
        for path in self.directory.glob("*.json"):
            path.unlink(missing_ok=True)
