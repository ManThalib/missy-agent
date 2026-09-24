"""Multi-DEX Solana pool discovery, normalization, filtering, and scoring."""

import os as _os
import sys as _sys

_REPO_ROOT = _os.path.dirname(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
)
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)

from .candidate import Candidate
from .candidate_json_encoder import CandidateJSONEncoder
from .client import MeteoraClient, MultiDexClient, OrcaClient, RaydiumClient
from .config import DEFAULT_CONFIG, FilterConfig
from .constants import (
    METEORA_API_BASE,
    METEORA_DISCOVER_URL,
    ORCA_API_BASE,
    RAYDIUM_API_BASE,
    RAYDIUM_LIST_V2,
    SOLANA_RPC_URL,
)
from .display import print_results, truncate
from .multi_dex_screener import (
    MeteoraScreener,
    MultiDexScreener,
    RaydiumScreener,
    normalize_pool,
)
from .scoring import PoolScoreInput, PoolScorer, ScoreBreakdown, ScoringConfig
from .token_config_parser import TokenConfigParser
from .token_set import TokenSet
from .whitelist import Whitelist

__all__ = [
    "RAYDIUM_API_BASE",
    "RAYDIUM_LIST_V2",
    "ORCA_API_BASE",
    "METEORA_API_BASE",
    "SOLANA_RPC_URL",
    "METEORA_DISCOVER_URL",
    "Candidate",
    "CandidateJSONEncoder",
    "PoolScorer",
    "PoolScoreInput",
    "ScoreBreakdown",
    "ScoringConfig",
    "FilterConfig",
    "DEFAULT_CONFIG",
    "Whitelist",
    "TokenSet",
    "TokenConfigParser",
    "RaydiumClient",
    "OrcaClient",
    "MultiDexClient",
    "MeteoraClient",
    "RaydiumScreener",
    "MultiDexScreener",
    "MeteoraScreener",
    "normalize_pool",
    "truncate",
    "print_results",
]
