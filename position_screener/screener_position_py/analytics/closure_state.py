"""Persistent closure-state tracker for wallet position scans.

Keeps a small JSON snapshot of ``{position_address: closed_at_timestamp}``
between CLI runs so that the display logic can determine whether a position
was closed *since the last scan* and therefore whether to render it under
the ``--show-inactive`` flag.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import TYPE_CHECKING, Dict, List, Sequence, Tuple

if TYPE_CHECKING:
    from ..liquidity_position import LiquidityPosition


class ClosureState:
    """Load/save the set of position addresses that were closed in the
    previous scan, keyed by ``(dex, position_address)``."""

    def __init__(self, path: str = "position_closure_state.json") -> None:
        self.path = path
        self._prev: Dict[str, int] = {}  # (dex, addr) -> closed_at unix sec

    # ----- loading / saving ------------------------------------------------

    def _load(self) -> Dict[str, int]:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as state_file:
                    payload = json.load(state_file)
            except (json.JSONDecodeError, OSError):
                return {}
            if not isinstance(payload, dict):
                return {}
            result = {}
            for key, value in payload.items():
                try:
                    result[str(key)] = int(value)
                except (TypeError, ValueError, OverflowError):
                    continue
            return result
        return {}

    def _save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".position-closure-", suffix=".json", dir=directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(self._prev, state_file, indent=2, sort_keys=True)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary_path, self.path)
        except OSError:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise

    def refresh(self, positions: List["LiquidityPosition"]) -> List[Tuple[int, str]]:
        """Compare *positions* against the previous scan and return a list of
        ``(closed_at, key)`` for any position that transitioned to ``closed``
        since the last scan.

        Returns newly-closed entries so the caller can decide to display them.
        """
        raw = self._load()
        prev = dict(raw)
        self._prev = dict(prev)

        now = int(time.time())
        newly_closed: List[Tuple[int, str]] = []

        positions_by_key = {
            f"{pos.dex}:{pos.position_address}": pos for pos in positions
        }
        for key, pos in positions_by_key.items():
            if pos.status == "closed":
                closed_ts = int(pos.closed_at) if pos.closed_at is not None else now
                # If this position was not previously closed, or was closed
                # at a later block time, record it as newly closed.
                if key not in prev or closed_ts > prev[key]:
                    newly_closed.append((closed_ts, key))
            # If position is active/inactive we don't flag it as "new close"

        # Merge newly closed into persistent store
        for _, key in newly_closed:
            closed_at = positions_by_key[key].closed_at
            self._prev[key] = int(closed_at) if closed_at is not None else now

        self._save()
        return newly_closed

    def is_newly_closed(
        self, position: "LiquidityPosition", newly_closed_keys: Sequence[str]
    ) -> bool:
        """Return ``True`` if *position* appears in the *newly_closed_keys* list."""
        key = f"{position.dex}:{position.position_address}"
        return key in newly_closed_keys

    def snapshot_keys(self) -> List[str]:
        """Return all position keys currently tracked as closed."""
        return list(self._prev.keys())


# ---------------------------------------------------------------------------
# Convenience helper used by the CLI wrapper
# ---------------------------------------------------------------------------


def scan_closure_state(
    positions: List["LiquidityPosition"],
    state_path: str = "position_closure_state.json",
) -> Tuple[ClosureState, List[str]]:
    """Initialize (or load) a ``ClosureState`` from *positions* and return
    the object plus a list of position keys that are newly closed since the
    prior scan.

    The returned *newly_closed_keys* list can be passed to the display
    module to decide whether to render closed-position rows.
    """
    closure_state = ClosureState(path=state_path)
    newly_closed = closure_state.refresh(positions)
    newly_closed_keys = [key for _, key in newly_closed]
    return closure_state, newly_closed_keys
