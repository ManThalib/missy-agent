#!/usr/bin/env python3
"""Regression tests for the pool screener hardening changes."""

import contextlib
import io
import tempfile
import unittest
from unittest.mock import Mock, patch

import screener_py  # noqa: F401  (puts the repo root on sys.path for core)
from core.cache import DiskCache
from core.constants import KNOWN_MINT_SYMBOLS, SOL_MINT, USDC_MINT, WBTC_MINT
from core.solana import is_valid_solana_address
from screener_py.candidate import Candidate
from screener_py.client import MultiDexClient
from screener_py.clients.raydium_client import RaydiumClient
from screener_py.display import print_results
from screener_py.filter_config import FilterConfig
from screener_py.multi_dex_screener import MultiDexScreener
from screener_py.pool_scorer import PoolScorer
from screener_py.pool_score_input import PoolScoreInput
from screener_py.scoring_config import ScoringConfig
from screener_py.tick_fetch import UNKNOWN_TICK, _batch_fetch_ticks
from screener_py.token_set import TokenSet
from screener_py.whitelist import Whitelist

FAKE_SOL = "FakeSo1Mint0000000000000000000000000000000"


def _token(symbol, address):
    return {"symbol": symbol, "address": address, "decimals": 9, "price_usd": 0.0}


class TestTokenMatching(unittest.TestCase):
    def test_known_symbol_with_a_different_mint_is_rejected(self):
        targets = TokenSet(["SOL"])
        self.assertTrue(targets.matches(_token("SOL", SOL_MINT)))
        self.assertFalse(targets.matches(_token("SOL", FAKE_SOL)))

    def test_symbol_without_a_known_mint_still_matches_by_symbol(self):
        targets = TokenSet(["JUP"])
        self.assertTrue(targets.matches(_token("JUP", "SomeJupMint111111111111111111111111111111")))

    def test_wbtc_matches_by_canonical_mint_and_by_alias(self):
        targets = TokenSet([WBTC_MINT])
        self.assertTrue(targets.matches(_token("WBTC", WBTC_MINT)))
        self.assertFalse(targets.matches(_token("WBTC", FAKE_SOL)))
        by_symbol = TokenSet(["WBTC"])
        self.assertIn(WBTC_MINT, by_symbol.addresses)

    def test_every_mint_in_the_canonical_map_is_valid(self):
        for mint in KNOWN_MINT_SYMBOLS:
            self.assertTrue(is_valid_solana_address(mint), mint)

    def test_whitelist_rejects_look_alike_paired_token(self):
        wl = Whitelist(target_tokens=["JUP"], allowed_paired_tokens=["USDC"])
        ok, _, _, _ = wl.validate_pair(
            _token("JUP", "SomeJupMint111111111111111111111111111111"),
            _token("USDC", FAKE_SOL),
        )
        self.assertFalse(ok)


class TestFeeCaptureUnits(unittest.TestCase):
    def test_window_total_is_converted_to_daily_fee(self):
        scorer = PoolScorer(ScoringConfig())

        def observation(fee_usd, days, volume):
            return PoolScoreInput(
                tvl_usd=100_000.0,
                fee_usd=fee_usd,
                volume_usd=volume,
                window_days=days,
                volatility_pct=10.0,
                pool_type="Concentrated",
            )

        # Same daily economics: 700 fees over 7 days equals 100 over 1 day,
        # and volume scales with the window too.
        weekly = scorer.score_fee_capture(observation(700.0, 7.0, volume=3_500_000.0))
        daily = scorer.score_fee_capture(observation(100.0, 1.0, volume=500_000.0))
        self.assertAlmostEqual(weekly["fc_score"], daily["fc_score"], places=6)
        self.assertAlmostEqual(
            weekly["absolute_fee_score"], daily["absolute_fee_score"], places=6
        )


class TestDisplayPartition(unittest.TestCase):
    def _candidate(self, name, eligible, reason=""):
        return Candidate(
            pool_address=name,
            name=name,
            whitelisted_token_symbol="JUP",
            whitelisted_token_address="",
            paired_token_symbol="USDC",
            tvl=50_000.0,
            fee_tvl_ratio=0.1,
            daily_fee_usd=50.0,
            volume_window=10_000.0,
            bin_step=0,
            fee_pct=0.3,
            volatility=1.0,
            score=50.0,
            dex="orca",
            eligible=eligible,
            rejected_reason=reason,
        )

    def test_policy_failed_pools_are_not_listed_as_qualified(self):
        candidates = [
            self._candidate("GOOD", True),
            self._candidate("BAD", False, "tvl $1,000 < policy $25,000"),
        ]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            print_results(candidates, {})
        out = buffer.getvalue()
        self.assertIn("FOUND 1 QUALIFIED POOL(S)", out)
        self.assertIn("1 pool(s) matched the whitelist but failed policy gates", out)
        self.assertIn("tvl: 1 pool(s)", out)
        self.assertNotIn("BAD", out.split("failed policy")[0])


class _DeadRpc:
    """RPC whose batch calls always fail."""

    def __init__(self):
        self.batch_calls = 0
        self.single_calls = 0

    def batch_partial(self, calls):
        self.batch_calls += 1
        raise RuntimeError("connection refused")

    def get_account_info(self, address):
        self.single_calls += 1
        raise RuntimeError("connection refused")


class TestTickBudgetAndCircuitBreaker(unittest.TestCase):
    def test_planned_backoff_never_exceeds_budget(self):
        sleeps = []
        with patch("time.sleep", side_effect=sleeps.append):
            result = _batch_fetch_ticks(
                _DeadRpc(),
                [("meteora", "PoolA1111111111111111111111111111111111111")],
                retries=6,
                backoff_seconds=1.5,
                budget_seconds=5.0,
            )
        self.assertEqual(list(result.values()), [UNKNOWN_TICK])
        self.assertLessEqual(sum(sleeps), 5.0 + 1e-9)

    def test_dead_rpc_skips_per_pool_fallbacks(self):
        rpc = _DeadRpc()
        screener = MultiDexScreener(
            whitelist=Whitelist(target_tokens=["SOL"], allowed_paired_tokens=["USDC"]),
            config=FilterConfig(),
            client=Mock(),
            rpc=rpc,
        )
        screener._jupiter_client = Mock(fetch_prices=Mock(return_value={}))
        pools = [
            _meteora_pool(f"Pool{i}1111111111111111111111111111111111111") for i in range(3)
        ]
        with patch("time.sleep"):
            screener.screen_all(pools)
        self.assertEqual(rpc.single_calls, 0)
        self.assertTrue(any("RPC tick lookup failed" in w for w in screener.warnings))


class TestRpcRequestSkipping(unittest.TestCase):
    def test_meteora_pool_with_api_bin_is_not_fetched_over_rpc(self):
        rpc = Mock()
        screener = MultiDexScreener(
            whitelist=Whitelist(target_tokens=["SOL"], allowed_paired_tokens=["USDC"]),
            config=FilterConfig(),
            client=Mock(),
            rpc=rpc,
        )
        screener._jupiter_client = Mock(fetch_prices=Mock(return_value={}))
        pool = _meteora_pool("ApiBinPool11111111111111111111111111111111", active_id=-5308)
        candidates, _ = screener.screen_all([pool])
        rpc.batch_partial.assert_not_called()
        self.assertEqual(candidates[0].active_bin_id, -5308)
        self.assertEqual(candidates[0].tick_source, "discovery_api")


def _meteora_pool(address, active_id=0):
    return {
        "_dex": "meteora",
        "address": address,
        "pool_type": "DLMM",
        "tvl": 50_000.0,
        "fee": 200.0,
        "volume": 100_000.0,
        "apr": 10.0,
        "fee_pct": 0.3,
        "pool_price": 1.0,
        "min_price": 0.9,
        "max_price": 1.1,
        "dlmm_params": {"bin_step": 10},
        "token_x": {"address": SOL_MINT, "symbol": "SOL", "decimals": 9, "price_usd": 0.0},
        "token_y": {"address": USDC_MINT, "symbol": "USDC", "decimals": 6, "price_usd": 0.0},
        "activeId": active_id,
    }


class TestRaydiumTargetPaging(unittest.TestCase):
    def test_full_pages_are_followed_up_to_max_pages(self):
        client = RaydiumClient(target_mints=["MintA"])
        config = FilterConfig(page_size=2)
        full = [{"id": "p1"}, {"id": "p2"}]
        with patch.object(client, "fetch_pools_by_mint", side_effect=[full, full, [{"id": "p3"}]]) as fetch, \
                patch.object(client, "_pause"):
            pools = client._fetch_by_target_mints(config, max_pages=5)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual({p["id"] for p in pools}, {"p1", "p2", "p3"})

    def test_max_pages_caps_the_requests(self):
        client = RaydiumClient(target_mints=["MintA"])
        full = [{"id": "x"}]
        with patch.object(client, "fetch_pools_by_mint", return_value=full) as fetch, \
                patch.object(client, "_pause"):
            client._fetch_by_target_mints(FilterConfig(page_size=1), max_pages=2)
        self.assertEqual(fetch.call_count, 2)


class TestWatchModeCache(unittest.TestCase):
    def test_cache_ttl_reaches_every_provider(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        client = MultiDexClient(cache=DiskCache(directory=tmp.name), cache_ttl_seconds=10.0)
        self.assertEqual(client.cache_ttl_seconds, 10.0)
        for provider in client.clients.values():
            self.assertEqual(provider.cache_ttl_seconds, 10.0)


class TestCliFlags(unittest.TestCase):
    def setUp(self):
        import pool_screener as cli

        self.cli = cli

    def test_negative_watch_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.cli.parse_args(["--watch", "-5"])

    def test_unenforced_threshold_flags_warn(self):
        args = self.cli.parse_args(["--min-apr", "5", "--dex", "meteora"])
        warnings = self.cli.unenforced_flag_warnings(args)
        self.assertTrue(any("--min-apr" in w for w in warnings))

    def test_defaults_produce_no_warnings(self):
        args = self.cli.parse_args(["--dex", "meteora"])
        self.assertEqual(self.cli.unenforced_flag_warnings(args), [])


if __name__ == "__main__":
    unittest.main()
