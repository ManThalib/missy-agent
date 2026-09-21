"""Pool discovery and screening configuration."""

from dataclasses import dataclass, fields
from typing import Any

TIMEFRAME_WINDOWS = {"24h": "day", "7d": "week", "30d": "month"}


@dataclass
class FilterConfig:
    """Configurable thresholds for pool liquidity, yield, and risk."""

    min_tvl: float = 10000.0
    max_tvl: float = 0.0
    min_fee_tvl: float = 0.05
    min_daily_fee: float = 20.0
    min_volume_usd: float = 5000.0
    min_apr: float = 0.0
    min_bin_step: int = 0
    max_bin_step: int = 0
    min_fee_pct: float = 0.0
    max_fee_pct: float = 0.0
    max_volatility: float = 5.0
    max_turnover_ratio: float = 50.0
    timeframe: str = "24h"
    pool_type: str = "all"
    dex: str = "all"
    pages: int = 5
    page_size: int = 100
    sort_field: str = "liquidity"
    timeout: float = 15.0

    @property
    def window(self) -> str:
        return TIMEFRAME_WINDOWS.get(self.timeframe, "day")

    @classmethod
    def from_args(cls, args: Any) -> "FilterConfig":
        """Create a configuration from an argparse-like namespace."""
        defaults = cls()
        values = {
            field.name: getattr(args, field.name, getattr(defaults, field.name))
            for field in fields(cls)
        }
        values["min_volume_usd"] = getattr(args, "min_volume", defaults.min_volume_usd)
        values["sort_field"] = getattr(args, "sort", defaults.sort_field)
        return cls(**values)
