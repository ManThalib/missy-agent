"""Shared normalization and coercion helpers for provider payloads."""

from typing import Any, Dict, Mapping, Optional


def mapping(value: Any) -> Mapping[str, Any]:
    """Return *value* if it is a mapping, otherwise an empty dict."""
    return value if isinstance(value, Mapping) else {}


def finite_float(value: Any, field: str) -> float:
    """Return a finite float from *value* or 0 if missing; raise on invalid data."""
    import math

    try:
        number = float(value if value is not None else 0.0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"invalid {field}: value must be finite")
    return number


def integer(value: Any, field: str) -> int:
    """Return an int from *value* or 0 if missing; raise on invalid data."""
    try:
        return int(value if value is not None else 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc


def token_entry(value: Any, default_address: str = "") -> Dict[str, Any]:
    """Normalize a token object into a common token_x/token_y shape."""
    data = mapping(value)
    return {
        "symbol": data.get("symbol", ""),
        "address": data.get("address", default_address),
        "decimals": finite_float(data.get("decimals"), "decimals") or 9,
        "price_usd": finite_float(data.get("price_usd"), "price_usd") or 0.0,
    }


def top_level_token_metadata(
    token_x: Mapping[str, Any], token_y: Mapping[str, Any]
) -> Dict[str, Any]:
    """Flatten token metadata for consumers that expect top-level fields."""
    return {
        "token_x_address": token_x.get("address", ""),
        "token_x_decimals": token_x.get("decimals", 9),
        "token_x_price_usd": token_x.get("price_usd", 0.0),
        "token_y_address": token_y.get("address", ""),
        "token_y_decimals": token_y.get("decimals", 9),
        "token_y_price_usd": token_y.get("price_usd", 0.0),
    }


def safe_get(mapping_obj: Mapping[str, Any], key: str, default: Any) -> Any:
    """Return a value from *mapping_obj* only if it is actually a mapping."""
    if not isinstance(mapping_obj, Mapping):
        return default
    return mapping_obj.get(key, default)
