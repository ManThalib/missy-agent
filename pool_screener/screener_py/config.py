"""Compatibility exports for screening configuration."""

from .filter_config import TIMEFRAME_WINDOWS, FilterConfig

DEFAULT_CONFIG = FilterConfig()

__all__ = ["DEFAULT_CONFIG", "FilterConfig", "TIMEFRAME_WINDOWS"]
