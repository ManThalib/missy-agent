#!/usr/bin/env python3
"""Unit tests for the multi-DEX pool screener."""

import unittest
from argparse import Namespace
from dataclasses import asdict, replace
from tempfile import NamedTemporaryFile
from unittest.mock import Mock

from screener_py.client import MultiDexClient
from screener_py.scoring import PoolScoreInput, PoolScorer
from screener_py.screener import (
    FilterConfig,
    RaydiumScreener,
    Whitelist,
    normalize_pool,
)
from screener_py.token_config_parser import TokenConfigParser

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class TestScreener(unittest.TestCase):
    def setUp(self):
        self.wl = Whitelist(
            target_tokens=["JUP", "BONK"],
            allowed_paired_tokens=["SOL", "USDC", SOL_MINT, USDC_MINT],
        )
        self.config = FilterConfig(
            min_tvl=10000.0,
            min_fee_tvl=0.05,
            min_daily_fee=20.0,
            max_volatility=20.0,
        )
        self.screener = RaydiumScreener(whitelist=self.wl, config=self.config)

    def sample_pool(self):
        # Raw Raydium API v3 shape
        return {
            "id": "PoolAddr1111111111111111111111111111111111",
            "type": "Concentrated",
            "tvl": 50000.0,
            "price": 0.5,
            "feeRate": 0.0025,
            "mintA": {
                "symbol": "JUP",
                "address": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
            },
            "mintB": {"symbol": "WSOL", "address": SOL_MINT},
            "day": {
                "volume": 25000.0,
                "volumeFee": 600.0,
                "apr": 400.0,
                "feeApr": 400.0,
                "priceMin": 0.48,
                "priceMax": 0.52,
            },
            "config": {"tickSpacing": 10},
        }

    def test_paired_token_allowed(self):
        pool = self.sample_pool()
        cand, reason = self.screener.screen_pool(pool)
        self.assertIsNotNone(cand, f"Expected pass, got: {reason}")
        self.assertEqual(cand.whitelisted_token_symbol, "JUP")
        self.assertEqual(cand.paired_token_symbol, "SOL")  # WSOL normalized

    def test_paired_token_rejected_when_not_allowed(self):
        pool = self.sample_pool()
        pool["mintB"] = {
            "symbol": "PEPE",
            "address": "PepeAddress111111111111111111111111111111111",
        }
        cand, reason = self.screener.screen_pool(pool)
        self.assertIsNone(cand)
        self.assertIn("not an allowed paired asset", reason)

    def test_inverted_pair_orientation(self):
        pool = self.sample_pool()
        pool["mintA"], pool["mintB"] = pool["mintB"], pool["mintA"]
        cand, reason = self.screener.screen_pool(pool)
        self.assertIsNotNone(cand, f"Expected pass, got: {reason}")
        self.assertEqual(cand.whitelisted_token_symbol, "JUP")
        self.assertEqual(cand.paired_token_symbol, "SOL")

    def test_from_file_json(self):
        wl = Whitelist.from_file("tokens.json")
        self.assertTrue(wl.targets.matches({"symbol": "SOL"}))
        self.assertTrue(wl.targets.matches({"symbol": "ETH"}))
        self.assertTrue(wl.paired.matches({"symbol": "SOL"}))
        self.assertTrue(wl.paired.matches({"symbol": "USDC"}))

    def test_quoted_and_literal_token_entries(self):
        payload = (
            '{"target_tokens": ['
            '{"asset": "\\"wSOL\\""}, '
            '{"symbol": "  My Token / USD  "}, '
            '{"asset": "Asset, With Comma"}, '
            '{"asset": "\\"cbXRP"}, null, 42], '
            '"allowed_paired_tokens": [{"symbol": "USDC"}]}'
        )
        with NamedTemporaryFile(mode="w", suffix=".json") as config_file:
            config_file.write(payload)
            config_file.flush()
            whitelist = Whitelist.from_file(config_file.name)

        self.assertTrue(whitelist.targets.matches({"symbol": "WSOL"}))
        self.assertTrue(whitelist.targets.matches({"symbol": "My Token / USD"}))
        self.assertTrue(whitelist.targets.matches({"symbol": "Asset, With Comma"}))
        self.assertTrue(whitelist.targets.matches({"symbol": "cbXRP"}))

    def test_missing_or_invalid_token_arrays_use_safe_fallbacks(self):
        with NamedTemporaryFile(mode="w", suffix=".json") as config_file:
            config_file.write('{"target_tokens": null, "allowed_paired_tokens": 123}')
            config_file.flush()
            whitelist = Whitelist.from_file(config_file.name)

        self.assertFalse(whitelist.targets.matches({"symbol": "SOL"}))
        self.assertTrue(whitelist.paired.matches({"symbol": "SOL"}))

    def test_screen_pool_low_tvl(self):
        pool = self.sample_pool()
        pool["tvl"] = 4000.0
        cand, reason = self.screener.screen_pool(pool)
        self.assertIsNone(cand)
        self.assertIn("TVL", reason)

    def test_zero_volume_fails_positive_minimum(self):
        pool = self.sample_pool()
        pool["day"]["volume"] = 0

        candidate, reason = self.screener.screen_pool(pool)

        self.assertIsNone(candidate)
        self.assertIn("volume", reason)

    def test_malformed_pool_does_not_discard_valid_pool(self):
        malformed = self.sample_pool()
        malformed["tvl"] = "not-a-number"

        candidates, rejects = self.screener.screen_all([malformed, self.sample_pool()])

        self.assertEqual(len(candidates), 1)
        self.assertTrue(
            any(reason.startswith("invalid provider payload") for reason in rejects)
        )

    def test_standard_pool_skips_tick_gate(self):
        pool = self.sample_pool()
        pool["type"] = "Standard"
        pool["config"] = {}
        cfg = FilterConfig(
            min_tvl=10000.0,
            min_fee_tvl=0.05,
            min_daily_fee=20.0,
            min_bin_step=5,
            max_bin_step=120,
            max_volatility=50.0,
        )
        s = RaydiumScreener(whitelist=self.wl, config=cfg)
        cand, reason = s.screen_pool(pool)
        self.assertIsNotNone(
            cand, f"Standard pool should skip tick gate, got: {reason}"
        )

    def test_tick_spacing_gate_for_clmm(self):
        pool = self.sample_pool()
        pool["config"] = {"tickSpacing": 200}
        cfg = FilterConfig(
            min_tvl=1000.0,
            min_fee_tvl=0.0,
            min_daily_fee=0.0,
            min_volume_usd=0.0,
            max_bin_step=120,
            max_volatility=100.0,
        )
        s = RaydiumScreener(whitelist=self.wl, config=cfg)
        cand, reason = s.screen_pool(pool)
        self.assertIsNone(cand)
        self.assertIn("tick spacing", reason)

    def test_orca_pool_normalization(self):
        pool = {
            "_dex": "orca",
            "address": "OrcaPool111111111111111111111111111111111",
            "tickSpacing": 64,
            "feeRate": 3000,
            "tvlUsdc": "50000",
            "price": "1",
            "priceHistory7d": ["0.98", "1.02"],
            "tokenA": {
                "symbol": "JUP",
                "address": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
            },
            "tokenB": {"symbol": "SOL", "address": SOL_MINT},
            "stats": {
                "24h": {"volume": "25000", "fees": "600", "yieldOverTvl": "0.012"}
            },
        }
        normalized = normalize_pool(pool)
        self.assertEqual(normalized["dex"], "orca")
        self.assertEqual(normalized["tick_spacing"], 64)
        self.assertAlmostEqual(normalized["fee_pct"], 0.3)

    def test_meteora_pool_normalization(self):
        pool = {
            "_dex": "meteora",
            "pool_address": "MeteoraPool111111111111111111111111111111",
            "token_x": {
                "symbol": "JUP",
                "address": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
            },
            "token_y": {"symbol": "SOL", "address": SOL_MINT},
            "dlmm_params": {"bin_step": 20},
        }
        normalized = normalize_pool(pool)
        self.assertEqual(normalized["dex"], "meteora")
        self.assertEqual(normalized["tick_spacing"], 20)

    def test_candidate_serialization_matches_dataclass_shape(self):
        candidate, _ = self.screener.screen_pool(self.sample_pool())
        self.assertEqual(candidate.to_dict(), asdict(candidate))

    def test_candidate_serialization_detaches_wallet_positions(self):
        candidate, _ = self.screener.screen_pool(self.sample_pool())
        candidate.wallet_positions = [{"status": "active"}]

        serialized = candidate.to_dict()
        serialized["wallet_positions"][0]["status"] = "closed"

        self.assertEqual(candidate.wallet_positions[0]["status"], "active")

    def test_token_object_uses_first_valid_string_value(self):
        self.assertEqual(
            TokenConfigParser._parse_entry({"symbol": None, "address": SOL_MINT}),
            SOL_MINT,
        )

    def test_config_from_args_includes_discovery_fields(self):
        config = FilterConfig.from_args(Namespace(page_size=321, sort="fee24h"))
        self.assertEqual(config.page_size, 321)
        self.assertEqual(config.sort_field, "fee24h")

    def test_multi_dex_fetch_is_concurrent_and_ordered(self):
        client = MultiDexClient()

        client.clients = {
            "raydium": Mock(fetch_pools=Mock(return_value=[{"id": "raydium"}])),
            "orca": Mock(fetch_pools=Mock(return_value=[{"id": "orca"}])),
            "meteora": Mock(fetch_pools=Mock(return_value=[{"id": "meteora"}])),
        }
        pools = client.fetch_pools(FilterConfig(dex="all"))
        self.assertEqual([pool["id"] for pool in pools], ["raydium", "orca", "meteora"])

    def test_empty_success_does_not_become_total_provider_failure(self):
        client = MultiDexClient()
        client.clients = {
            "raydium": Mock(fetch_pools=Mock(return_value=[])),
            "orca": Mock(fetch_pools=Mock(side_effect=RuntimeError("down"))),
            "meteora": Mock(fetch_pools=Mock(side_effect=RuntimeError("down"))),
        }

        self.assertEqual(client.fetch_pools(FilterConfig(dex="all")), [])
        self.assertEqual(set(client.errors), {"orca", "meteora"})

    def test_pool_score_is_provider_independent(self):
        scorer = PoolScorer()
        observations = [
            PoolScoreInput(
                tvl_usd=100000.0,
                fee_usd=100.0,
                volume_usd=50000.0,
                window_days=1.0,
                reported_apr_pct=40.0,
                volatility_pct=4.0,
                fee_tier_pct=0.3,
                pool_type="Whirlpool",
            ),
            PoolScoreInput(
                tvl_usd=100000.0,
                fee_usd=100.0,
                volume_usd=50000.0,
                window_days=1.0,
                reported_apr_pct=40.0,
                volatility_pct=4.0,
                fee_tier_pct=0.3,
                pool_type="DLMM",
            ),
            PoolScoreInput(
                tvl_usd=100000.0,
                fee_usd=100.0,
                volume_usd=50000.0,
                window_days=1.0,
                reported_apr_pct=40.0,
                volatility_pct=4.0,
                fee_tier_pct=0.3,
                pool_type="Concentrated",
            ),
        ]
        scores = [scorer.score(observation).total for observation in observations]
        self.assertAlmostEqual(scores[0], scores[1])
        self.assertAlmostEqual(scores[1], scores[2])

    def test_pool_score_normalizes_window_length(self):
        scorer = PoolScorer()
        daily = PoolScoreInput(
            tvl_usd=100000.0,
            fee_usd=100.0,
            volume_usd=50000.0,
            window_days=1.0,
            reported_apr_pct=40.0,
            volatility_pct=4.0,
            pool_type="Standard",
        )
        weekly = PoolScoreInput(
            tvl_usd=100000.0,
            fee_usd=700.0,
            volume_usd=350000.0,
            window_days=7.0,
            reported_apr_pct=40.0,
            volatility_pct=4.0,
            pool_type="Standard",
        )
        self.assertAlmostEqual(scorer.score(daily).total, scorer.score(weekly).total)

    def test_projected_apr_is_capped_and_discounted(self):
        scorer = PoolScorer()
        base = PoolScoreInput(
            tvl_usd=100000.0,
            fee_usd=10.0,
            volume_usd=10000.0,
            window_days=1.0,
            volatility_pct=2.0,
            pool_type="Standard",
        )
        realistic = scorer.score(base)
        projected = scorer.score(replace(base, reported_apr_pct=10000.0))
        self.assertLess(projected.adjusted_apr, 20.0)
        self.assertLess(projected.risk_score, realistic.risk_score)

    def test_protocol_fee_share_reduces_realized_yield(self):
        scorer = PoolScorer()
        gross = PoolScoreInput(
            tvl_usd=100000.0,
            fee_usd=100.0,
            volume_usd=50000.0,
            window_days=1.0,
            lp_fee_share=1.0,
        )
        net = replace(gross, lp_fee_share=0.8)
        self.assertAlmostEqual(
            scorer.score(net).realized_fee_apr,
            scorer.score(gross).realized_fee_apr * 0.8,
        )


if __name__ == "__main__":
    unittest.main()
