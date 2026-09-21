#!/usr/bin/env python3
"""Unit tests for wallet position decoding and candidate enrichment."""

import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import Mock

from screener_position_py import (
    ClosureState,
    LiquidityPosition,
    attach_positions,
)
from screener_position_py.analytics.scoring import (
    PositionScoreBreakdown,
    PositionScoreInput,
    PositionScorer,
    PositionSnapshot,
)
from screener_position_py.analytics.trader_scoring import (
    TradePerformance,
    TraderPerformanceScorer,
)
from screener_position_py.helius_parser import (
    SYSTEM_PROGRAM,
    TOKEN_2022_PROGRAM,
    HeliusWebhookParser,
)
from screener_position_py.position_scan import PositionScan
from screener_position_py.position_scanner import (
    ORCA_PROGRAM,
    PositionScanner,
    _b58encode,
    _discriminator,
)
from screener_position_py.rpc_client import RpcClient
class Candidate:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.wallet_positions = []
from screener_position_py.display import print_positions


def candidate(dex="orca", pool="pool-a"):
    return Candidate(
        pool_address=pool,
        name="A-B",
        whitelisted_token_symbol="A",
        whitelisted_token_address="mint-a",
        paired_token_symbol="B",
        tvl=1.0,
        fee_tvl_ratio=0.0,
        daily_fee_usd=0.0,
        volume_window=0.0,
        bin_step=1,
        fee_pct=0.0,
        volatility=0.0,
        score=0.0,
        dex=dex,
    )


class TestPositions(unittest.TestCase):
    def test_orca_account_decoding(self):
        pool = bytes(range(32))
        mint = bytes(range(32, 64))
        data = bytearray(216)
        data[:8] = _discriminator("Position")
        data[8:40] = pool
        data[40:72] = mint
        data[72:88] = (123).to_bytes(16, "little")
        struct.pack_into("<ii", data, 88, -64, 128)
        data[108:116] = (7).to_bytes(8, "little")
        data[132:140] = (9).to_bytes(8, "little")
        data[156:164] = (11).to_bytes(8, "little")

        position = PositionScanner._decode_orca("position", bytes(data))

        self.assertEqual(position.pool_address, _b58encode(pool))
        self.assertEqual(position.position_mint, _b58encode(mint))
        self.assertEqual(position.status, "active")
        self.assertEqual(position.fees_owed_raw, [7, 9])
        self.assertEqual(position.rewards_owed_raw, [11, 0, 0])

    def test_attach_positions_matches_dex_and_pool(self):
        owned = candidate()
        same_address_other_dex = candidate(dex="raydium")
        positions = [
            LiquidityPosition("orca", "one", "pool-a", "active", liquidity_raw=10),
            LiquidityPosition("orca", "two", "pool-a", "inactive"),
            LiquidityPosition("orca", "closed", "pool-a", "closed"),
        ]

        attach_positions([owned, same_address_other_dex], positions)

        self.assertEqual(
            [item["position_address"] for item in owned.wallet_positions],
            ["one", "two"],
        )
        self.assertEqual(same_address_other_dex.wallet_positions, [])

    def test_history_builds_closed_lifecycle(self):
        scanner = PositionScanner(
            rpc=Mock(
                call=Mock(return_value=[]),
                batch=Mock(return_value=[]),
            ),
            helius_api_key="",
        )
        transactions = [
            {
                "signature": "open-sig",
                "parsed": {
                    "blockTime": 10,
                    "transactionStatus": "OK",
                    "instructions": [
                        {
                            "programId": ORCA_PROGRAM,
                            "instructionName": "open_position",
                            "decoded": {
                                "accounts": [
                                    {"name": "position", "pubkey": "position-a"},
                                    {"name": "whirlpool", "pubkey": "pool-a"},
                                    {"name": "position_mint", "pubkey": "mint-a"},
                                ]
                            },
                        }
                    ],
                },
            },
            {
                "signature": "close-sig",
                "parsed": {
                    "blockTime": 20,
                    "transactionStatus": "OK",
                    "instructions": [
                        {
                            "programId": ORCA_PROGRAM,
                            "instructionName": "close_position",
                            "decoded": {
                                "accounts": [
                                    {"name": "position", "pubkey": "position-a"}
                                ]
                            },
                        }
                    ],
                },
            },
        ]

        positions = scanner._parse_history(transactions, ["orca"])

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].status, "closed")
        self.assertEqual(positions[0].pool_address, "pool-a")
        self.assertEqual(positions[0].opened_signature, "open-sig")
        self.assertEqual(positions[0].closed_signature, "close-sig")

    def test_current_state_wins_over_history(self):
        historical = LiquidityPosition(
            "orca", "position-a", "pool-a", "closed", opened_signature="open"
        )
        current = LiquidityPosition(
            "orca", "position-a", "pool-a", "active", liquidity_raw=1
        )

        result = PositionScanner._merge_positions([current], [historical])

        self.assertEqual(result[0].status, "active")
        self.assertEqual(result[0].opened_signature, "open")


class TestPositionScorer(unittest.TestCase):
    def test_score_neutral_when_no_data(self):
        """All-zero position should still produce a valid score."""
        scorer = PositionScorer()
        inp = PositionScoreInput(
            dex="orca",
            position_address="pos-1",
            liquidity_raw=0,
            lower_bound=None,
            upper_bound=None,
            fees_owed_raw=[],
            rewards_owed_raw=[],
            status="active",
        )
        breakdown, total = scorer.score(inp)
        self.assertIsInstance(breakdown, PositionScoreBreakdown)
        self.assertIsInstance(total, float)
        self.assertGreaterEqual(total, 0)
        self.assertLessEqual(total, 100)

    def test_score_with_range_data(self):
        """Position inside range should have high range efficiency."""
        scorer = PositionScorer()
        inp = PositionScoreInput(
            dex="meteora",
            position_address="pos-2",
            liquidity_raw=1000,
            lower_bound=0,
            upper_bound=100,
            fees_owed_raw=[10, 20],
            rewards_owed_raw=[],
            status="active",
            current_price=50.0,
            lower_price=0.0,
            upper_price=100.0,
            observation_window_secs=3600,
        )
        breakdown, total = scorer.score(inp)
        self.assertGreater(breakdown.range_efficiency, 0)
        self.assertGreaterEqual(total, 0)
        self.assertLessEqual(total, 100)

    def test_score_boundary_proximity_penalty(self):
        """Position very close to boundary should get low boundary_risk score."""
        scorer = PositionScorer()
        # Position with price very close to upper bound
        inp = PositionScoreInput(
            dex="raydium",
            position_address="pos-3",
            liquidity_raw=500,
            lower_bound=0,
            upper_bound=100,
            fees_owed_raw=[5, 5],
            rewards_owed_raw=[],
            status="active",
            current_price=98.0,  # Within 2% of upper bound (100)
            lower_price=0.0,
            upper_price=100.0,
            observation_window_secs=3600,
        )
        breakdown, total = scorer.score(inp)
        self.assertEqual(breakdown.boundary_risk, 0.0)

    def test_score_fee_yield_annualized(self):
        """Fee yield rate should be computed as annualized APR."""
        scorer = PositionScorer()
        inp = PositionScoreInput(
            dex="orca",
            position_address="pos-4",
            liquidity_raw=1000,
            lower_bound=0,
            upper_bound=100,
            fees_owed_raw=[100, 0],
            rewards_owed_raw=[],
            status="active",
            position_value_quote=1000.0,
            fee_value_delta_quote=100.0,
            observation_window_secs=3600,
        )
        breakdown, total = scorer.score(inp)
        # With 100 fees in 1 hour on 1000 liquidity -> ~8760% APR -> capped at 1.0
        self.assertGreater(breakdown.fee_yield_rate, 0)
        self.assertLessEqual(breakdown.fee_yield_rate, 1.0)

    def test_position_snapshot_and_history(self):
        """Record snapshots and verify range-efficiency from history."""
        scorer = PositionScorer()
        snap1 = PositionSnapshot(
            dex="orca",
            position_address="pos-5",
            pool_address="pool-1",
            status="active",
            lower_bound=0,
            upper_bound=100,
            liquidity_raw=1000,
            fees_owed_raw=[],
            rewards_owed_raw=[],
            current_price=50.0,
            lower_price=0.0,
            upper_price=100.0,
        )
        snap2 = PositionSnapshot(
            dex="orca",
            position_address="pos-5",
            pool_address="pool-1",
            status="active",
            lower_bound=0,
            upper_bound=100,
            liquidity_raw=1000,
            fees_owed_raw=[],
            rewards_owed_raw=[],
            current_price=150.0,  # Out of range
            lower_price=0.0,
            upper_price=100.0,
        )
        scorer.record_snapshot(snap1)
        scorer.record_snapshot(snap2)
        efficiency = scorer._range_efficiency_from_history(("orca", "pos-5"), 3600)
        self.assertEqual(efficiency, 0.5)

    def test_closure_state_tracks_newly_closed(self):
        """ClosureState should detect positions closed since last scan."""
        import os
        import tempfile

        fd, tmp = tempfile.mkstemp(prefix="closure_state_test_", suffix=".json")
        os.close(fd)
        os.unlink(tmp)
        self.addCleanup(lambda: os.path.exists(tmp) and os.unlink(tmp))
        cstate = ClosureState(path=tmp)
        positions = [
            LiquidityPosition("orca", "pos-a", "pool-1", "closed", closed_at=1000),
        ]
        # First call records the close
        newly_closed = cstate.refresh(positions)
        self.assertEqual(len(newly_closed), 1)

        # Second call with same data should not flag as newly closed
        newly_closed2 = cstate.refresh(positions)
        self.assertEqual(len(newly_closed2), 0)

    def test_from_liquidity_position_factory(self):
        """Factory should produce correct PositionScoreInput from LiquidityPosition."""
        pos = LiquidityPosition(
            dex="meteora",
            position_address="pos-6",
            liquidity_raw=500,
            lower_bound=10,
            upper_bound=20,
            fees_owed_raw=[1, 2],
            rewards_owed_raw=[0, 0],
            status="active",
        )
        inp = PositionScoreInput(
            dex=pos.dex,
            position_address=pos.position_address,
            liquidity_raw=pos.liquidity_raw,
            lower_bound=pos.lower_bound,
            upper_bound=pos.upper_bound,
            fees_owed_raw=list(pos.fees_owed_raw),
            rewards_owed_raw=list(pos.rewards_owed_raw),
            status=pos.status,
        )
        self.assertEqual(inp.dex, "meteora")
        self.assertEqual(inp.liquidity_raw, 500)
        self.assertEqual(inp.fees_owed_raw, [1, 2])

    def test_breakdown_total_matches_returned_total(self):
        score_input = PositionScoreInput(
            dex="orca",
            position_address="pos-total",
            liquidity_raw=0,
            lower_bound=None,
            upper_bound=None,
            fees_owed_raw=[],
            rewards_owed_raw=[],
            status="inactive",
        )

        breakdown, total = PositionScorer().score(score_input)

        self.assertEqual(breakdown.total, total)

    def test_raw_fee_units_are_not_treated_as_quote_apr(self):
        score_input = PositionScoreInput(
            dex="orca",
            position_address="pos-raw-fees",
            liquidity_raw=1,
            lower_bound=None,
            upper_bound=None,
            fees_owed_raw=[10**12, 10**12],
            rewards_owed_raw=[],
            status="active",
        )

        breakdown, _ = PositionScorer().score(score_input)

        self.assertEqual(breakdown.fee_yield_rate, 0.5)


class TestPositionSafety(unittest.TestCase):
    def test_show_inactive_displays_inactive_position(self):
        scan = PositionScan(
            wallet="wallet",
            positions=[LiquidityPosition("orca", "inactive", status="inactive")],
        )
        output = StringIO()

        with redirect_stdout(output):
            print_positions(scan, show_inactive=True)

        self.assertIn("INACTIVE", output.getvalue())

    def test_position_display_shows_scoring_inputs_and_action(self):
        position = LiquidityPosition(
            "orca",
            "position-address-123456789",
            "pool-address-123456789",
            "active",
            fees_owed_raw=[10, 20],
            pool_enrichment={
                "current_price": 50.0,
                "lower_price": 0.0,
                "upper_price": 100.0,
                "position_value_quote": 10_000.0,
                "fee_value_delta_quote": 1.0,
                "observation_window_secs": 86_400,
            },
        )
        scan = PositionScan(wallet="wallet", positions=[position])
        output = StringIO()

        with redirect_stdout(output):
            print_positions(scan)

        rendered = output.getvalue()
        self.assertIn("POSITION", rendered)
        self.assertIn("FEE APR", rendered)
        self.assertIn("R/F/B", rendered)
        self.assertIn("2/2", rendered)
        self.assertIn("3.7%", rendered)
        self.assertIn("POSITION HEALTH SUMMARY", rendered)

    def test_position_display_flags_missing_score_enrichment(self):
        scan = PositionScan(
            wallet="wallet",
            positions=[LiquidityPosition("orca", "position", status="active")],
        )
        output = StringIO()

        with redirect_stdout(output):
            print_positions(scan)

        rendered = output.getvalue()
        self.assertIn("0/2", rendered)
        self.assertIn("Needs enrichment", rendered)

    def test_rpc_batch_rejects_missing_response_id(self):
        client = RpcClient("https://example.invalid")
        client._post = Mock(return_value=[])

        with self.assertRaisesRegex(RuntimeError, "missing id"):
            client.batch([("getSlot", [])])

    def test_closure_state_recovers_from_non_object_json(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as state_file:
            state_file.write("[]")
            state_path = state_file.name
        self.addCleanup(
            lambda: __import__("os").path.exists(state_path)
            and __import__("os").unlink(state_path)
        )

        state = ClosureState(state_path)
        newly_closed = state.refresh(
            [LiquidityPosition("orca", "closed", status="closed")]
        )

        self.assertEqual(len(newly_closed), 1)


class TestHeliusWebhookParser(unittest.TestCase):
    def test_spl_transfer_is_not_reported_as_native_sol(self):
        transaction = HeliusWebhookParser().parse_transaction(
            {
                "instructions": [
                    {
                        "programId": TOKEN_2022_PROGRAM,
                        "parsed": {
                            "type": "transfer",
                            "info": {
                                "source": "source-account-11111111111111111111",
                                "destination": "destination-account-1111111111111",
                                "amount": "1000",
                            },
                        },
                    },
                    {
                        "programId": SYSTEM_PROGRAM,
                        "parsed": {
                            "type": "transfer",
                            "info": {
                                "source": "source-account-11111111111111111111",
                                "destination": "destination-account-1111111111111",
                                "lamports": 500,
                            },
                        },
                    },
                ],
            }
        )

        self.assertEqual(len(transaction.native_transfers), 1)
        self.assertEqual(transaction.native_transfers[0].lamports, 500)

    def test_enhanced_swap_extracts_financial_and_route_fields(self):
        payload = [
            {
                "signature": "swap-sig",
                "slot": 55,
                "timestamp": 1000,
                "type": "SWAP",
                "source": "JUPITER",
                "fee": 12000,
                "feePayer": "wallet",
                "nativeTransfers": [
                    {
                        "fromUserAccount": "wallet",
                        "toUserAccount": "pool",
                        "amount": 2_000_000,
                    }
                ],
                "tokenTransfers": [
                    {
                        "mint": "mint-a",
                        "fromUserAccount": "pool",
                        "toUserAccount": "wallet",
                        "fromTokenAccount": "pool-token",
                        "toTokenAccount": "wallet-token",
                        "rawTokenAmount": {"tokenAmount": "900", "decimals": 6},
                        "programId": TOKEN_2022_PROGRAM,
                        "feeAmount": {"amount": "10", "decimals": 6},
                    }
                ],
                "accountData": [
                    {
                        "account": "wallet",
                        "nativeBalanceChange": -2_012_000,
                        "tokenBalanceChanges": [
                            {
                                "mint": "mint-a",
                                "tokenAccount": "wallet-token",
                                "userAccount": "wallet",
                                "rawTokenAmount": {"tokenAmount": "900", "decimals": 6},
                                "programId": TOKEN_2022_PROGRAM,
                            }
                        ],
                    }
                ],
                "events": {
                    "swap": {
                        "nativeInput": {"account": "wallet", "amount": "2000000"},
                        "tokenOutputs": [
                            {
                                "mint": "mint-a",
                                "userAccount": "wallet",
                                "tokenAccount": "wallet-token",
                                "rawTokenAmount": {"tokenAmount": "900", "decimals": 6},
                            }
                        ],
                    }
                },
                "instructions": [
                    {
                        "programId": "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
                        "instructionName": "swap",
                    }
                ],
            }
        ]

        transaction = HeliusWebhookParser().parse_payload(payload)[0]

        self.assertEqual(transaction.priority_fee_lamports, 7000)
        self.assertEqual(transaction.token_transfers[0].amount_raw, 900)
        self.assertEqual(transaction.token_balance_deltas[0].amount_raw, 900)
        self.assertEqual(transaction.token_fees[0].amount_raw, 10)
        self.assertEqual(transaction.swap.inputs[0].mint, "SOL")
        self.assertEqual(transaction.swap.outputs[0].mint, "mint-a")
        self.assertEqual(transaction.swap.route, ("JUPITER", "RAYDIUM_CLMM"))
        self.assertEqual(transaction.swap.counterparties, ("pool",))

    def test_raw_transaction_extracts_meta_deltas_and_inner_instructions(self):
        payload = {
            "blockTime": 2000,
            "slot": 77,
            "meta": {
                "err": None,
                "fee": 15000,
                "computeUnitsConsumed": 123456,
                "preBalances": [100000, 5],
                "postBalances": [85000, 5],
                "preTokenBalances": [
                    {
                        "accountIndex": 1,
                        "mint": "mint",
                        "owner": "wallet",
                        "programId": TOKEN_2022_PROGRAM,
                        "uiTokenAmount": {"amount": "100", "decimals": 2},
                    }
                ],
                "postTokenBalances": [
                    {
                        "accountIndex": 1,
                        "mint": "mint",
                        "owner": "wallet",
                        "programId": TOKEN_2022_PROGRAM,
                        "uiTokenAmount": {"amount": "75", "decimals": 2},
                    }
                ],
                "rewards": [
                    {
                        "pubkey": "rent-account",
                        "lamports": 2039280,
                        "rewardType": "Rent",
                    }
                ],
                "innerInstructions": [
                    {
                        "index": 0,
                        "instructions": [
                            {
                                "programIdIndex": 2,
                                "accounts": [0, 1],
                                "data": "abc",
                            }
                        ],
                    }
                ],
                "loadedAddresses": {"writable": ["loaded-program"], "readonly": []},
            },
            "transaction": {
                "signatures": ["raw-sig"],
                "message": {
                    "header": {"numRequiredSignatures": 1},
                    "accountKeys": ["wallet", "token-account"],
                    "instructions": [],
                },
            },
        }

        transaction = HeliusWebhookParser().parse_payload(payload)[0]

        self.assertEqual(transaction.signature, "raw-sig")
        self.assertEqual(transaction.compute_units_consumed, 123456)
        self.assertEqual(transaction.priority_fee_lamports, 10000)
        self.assertEqual(transaction.account_balance_deltas[0].delta_lamports, -15000)
        self.assertEqual(transaction.token_balance_deltas[0].amount_raw, -25)
        self.assertEqual(transaction.rent_adjustments[0].lamports, 2039280)
        self.assertTrue(transaction.instructions[0].inner)
        self.assertEqual(transaction.instructions[0].program_id, "loaded-program")


class TestTraderPerformanceScorer(unittest.TestCase):
    def test_costs_drawdown_and_recency_affect_score(self):
        scorer = TraderPerformanceScorer(half_life_days=10)
        old_win = TradePerformance(
            timestamp=0,
            gross_pnl=100,
            capital=1000,
            volume=20000,
            liquidity=100000,
            network_fee=0,
            priority_fee=0,
            token_fee=0,
            rent_adjustment=0,
            mev_suspected=False,
        )
        recent_loss = TradePerformance(
            timestamp=9 * 86400,
            gross_pnl=-100,
            capital=1000,
            volume=20000,
            liquidity=100000,
            network_fee=2,
            priority_fee=1,
            token_fee=0,
            rent_adjustment=0,
            mev_suspected=False,
        )

        result = scorer.score([old_win, recent_loss], now=10 * 86400)

        self.assertLess(result.win_rate, 50.0)
        self.assertGreater(result.max_drawdown, 0.0)
        self.assertEqual(result.net_pnl, -3.0)
        self.assertEqual(result.total_costs, 3.0)
        self.assertLess(result.sharpe_ratio, 0.0)

    def test_dust_and_mev_profit_is_discounted(self):
        scorer = TraderPerformanceScorer()
        clean = TradePerformance(
            timestamp=100,
            gross_pnl=100,
            capital=1000,
            volume=20000,
            liquidity=100000,
            network_fee=0,
            priority_fee=0,
            token_fee=0,
            rent_adjustment=0,
            mev_suspected=False,
        )
        dust_mev = TradePerformance(
            timestamp=100,
            gross_pnl=100,
            capital=1000,
            volume=10,
            liquidity=100,
            network_fee=0,
            priority_fee=0,
            token_fee=0,
            rent_adjustment=0,
            mev_suspected=True,
        )

        clean_result = scorer.score([clean], now=100)
        dust_result = scorer.score([dust_mev], now=100)

        self.assertGreater(clean_result.adjusted_pnl, dust_result.adjusted_pnl)
        self.assertGreater(clean_result.final_score, dust_result.final_score)


if __name__ == "__main__":
    unittest.main()
