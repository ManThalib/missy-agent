"""Load and validate Missy's policy (eligibility gates + default scoring).

The policy is the single source of truth for what Missy emits. Missing file
falls back to the in-code defaults so the screener stays runnable; a present
but malformed file raises :class:`PolicyError` (fail-closed) rather than
silently running with the wrong gates.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

DEFAULT_POLICY: Dict[str, Any] = {
    "version": 1,
    "universe": {
        "stablecoins": ["USDC", "USDT", "USDG"],
        "high_caps": ["SOL", "WSOL", "BTC", "WBTC", "CBBTC", "ETH", "WETH"],
    },
    "pool_eligibility": {
        "min_tvl_usd": 25000.0,
        "min_volume_usd": 5000.0,
        "min_fee_tvl_ratio_pct": 0.05,
        "max_volatility_pct": 50.0,
        "max_turnover_ratio": 50.0,
    },
    "scoring": {
        "model": "missy-default",
        "version": 1,
        "weights": {"yield": 25.0, "depth": 25.0, "efficiency": 25.0, "risk": 25.0},
        "anchors": {
            "yield_apr_pct": 50.0,
            "depth_usd": 1000000.0,
            "turnover_daily": 2.0,
            "volatility_pct": 20.0,
            "high_fee_pct": 1.0,
        },
        "projected_apr": {"weight": 0.30, "premium_cap": 10.0, "multiple_cap": 2.0},
    },
}

# Repo root: .../pool_screener/ (two levels above this module).
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_POLICY_PATH = os.path.join(_REPO_DIR, "missy_policy.json")


class PolicyError(Exception):
    """Raised when missy_policy.json is present but invalid."""


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _require_number(container: Dict[str, Any], key: str, section: str) -> float:
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError(f"{section}.{key} must be a number, got {value!r}")
    return float(value)


def _validate(policy: Dict[str, Any]) -> None:
    elig = policy.get("pool_eligibility")
    if not isinstance(elig, dict):
        raise PolicyError("pool_eligibility must be an object")
    for key in (
        "min_tvl_usd",
        "min_volume_usd",
        "min_fee_tvl_ratio_pct",
        "max_volatility_pct",
        "max_turnover_ratio",
    ):
        _require_number(elig, key, "pool_eligibility")

    scoring = policy.get("scoring")
    if not isinstance(scoring, dict):
        raise PolicyError("scoring must be an object")
    weights = scoring.get("weights")
    if not isinstance(weights, dict):
        raise PolicyError("scoring.weights must be an object")
    for key in ("yield", "depth", "efficiency", "risk"):
        _require_number(weights, key, "scoring.weights")
    anchors = scoring.get("anchors")
    if not isinstance(anchors, dict):
        raise PolicyError("scoring.anchors must be an object")
    for key in ("yield_apr_pct", "depth_usd", "turnover_daily", "volatility_pct"):
        _require_number(anchors, key, "scoring.anchors")

    universe = policy.get("universe")
    if not isinstance(universe, dict):
        raise PolicyError("universe must be an object")
    for key in ("stablecoins", "high_caps"):
        values = universe.get(key)
        if not isinstance(values, list) or not all(
            isinstance(item, str) for item in values
        ):
            raise PolicyError(f"universe.{key} must be a list of strings")


def load_policy(path: Optional[str] = None) -> Dict[str, Any]:
    """Return the merged, validated policy dict.

    Missing file -> built-in defaults. Present-but-invalid -> PolicyError.
    """
    resolved = path or os.environ.get("MISSY_POLICY_FILE") or DEFAULT_POLICY_PATH
    if not resolved or not os.path.exists(resolved):
        return _deep_merge(DEFAULT_POLICY, {})

    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise PolicyError(f"cannot read policy {resolved}: {exc}") from exc

    if not isinstance(data, dict):
        raise PolicyError(f"policy {resolved} must be a JSON object")

    merged = _deep_merge(DEFAULT_POLICY, data)
    _validate(merged)
    return merged


def eligibility_gates(policy: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """Return the numeric eligibility gates from a policy (or the default)."""
    source = policy if policy is not None else load_policy()
    return dict(source["pool_eligibility"])
