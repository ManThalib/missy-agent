"""Simple on-disk TTL cache for provider responses and enrichment data."""

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_TTL_SECONDS = 300.0
DEFAULT_CACHE_DIR = "/tmp/missy-cache"


def cache_ttl_from_env(default: float = DEFAULT_TTL_SECONDS) -> float:
    """TTL for HTTP responses: ``MISSY_CACHE_TTL_SECONDS`` or *default*."""
    raw = os.environ.get("MISSY_CACHE_TTL_SECONDS", "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


class DiskCache:
    """Disk-backed cache with per-key TTL.

    Parameters
    ----------
    directory:
        Where to store cache entries. Defaults to ``$MISSY_CACHE_DIR`` or
        ``/tmp/missy-cache``. The directory is created with mode ``0700``.
    default_ttl_seconds:
        Time-to-live for entries that do not specify one.
    """

    def __init__(
        self,
        directory: Optional[str] = None,
        default_ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.directory = Path(
            directory or os.environ.get("MISSY_CACHE_DIR") or DEFAULT_CACHE_DIR
        )
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.default_ttl = default_ttl_seconds

    def _key(self, namespace: str, *parts: Any) -> str:
        # Length-prefix each part so ("a|b", "c") and ("a", "b|c") never collide.
        payload = "".join(
            f"{len(text)}:{text}" for text in (str(p) for p in parts)
        )
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
            # Write to a temp file and rename so readers never see partial JSON.
            descriptor, temporary = tempfile.mkstemp(
                prefix=".tmp-", suffix=".json", dir=self.directory
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(entry, handle, default=str)
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except (OSError, UnboundLocalError):
                pass

    def clear(self) -> None:
        for path in self.directory.glob("*.json"):
            path.unlink(missing_ok=True)
