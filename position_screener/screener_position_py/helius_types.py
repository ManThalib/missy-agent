"""Immutable, provider-independent records extracted from Helius payloads."""

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class NativeTransfer:
    __slots__ = ("from_account", "to_account", "lamports")

    from_account: str
    to_account: str
    lamports: int


@dataclass(frozen=True)
class TokenTransfer:
    __slots__ = (
        "mint",
        "from_user_account",
        "to_user_account",
        "from_token_account",
        "to_token_account",
        "amount_raw",
        "decimals",
        "token_program",
    )

    mint: str
    from_user_account: str
    to_user_account: str
    from_token_account: str
    to_token_account: str
    amount_raw: int
    decimals: int
    token_program: str


@dataclass(frozen=True)
class AccountBalanceDelta:
    __slots__ = ("account", "pre_lamports", "post_lamports", "delta_lamports")

    account: str
    pre_lamports: int
    post_lamports: int
    delta_lamports: int


@dataclass(frozen=True)
class TokenBalanceDelta:
    __slots__ = (
        "account",
        "account_index",
        "owner",
        "mint",
        "amount_raw",
        "decimals",
        "token_program",
    )

    account: str
    account_index: int
    owner: str
    mint: str
    amount_raw: int
    decimals: int
    token_program: str


@dataclass(frozen=True)
class TokenFee:
    __slots__ = ("mint", "amount_raw", "decimals", "token_program", "source")

    mint: str
    amount_raw: int
    decimals: int
    token_program: str
    source: str


@dataclass(frozen=True)
class NamedAccount:
    __slots__ = ("name", "address")

    name: str
    address: str


@dataclass(frozen=True)
class ParsedInstruction:
    __slots__ = (
        "program_id",
        "name",
        "accounts",
        "data",
        "amount_raw",
        "decimals",
        "inner",
    )

    program_id: str
    name: str
    accounts: Tuple[NamedAccount, ...]
    data: str
    amount_raw: int
    decimals: int
    inner: bool


@dataclass(frozen=True)
class SwapAsset:
    __slots__ = (
        "mint",
        "amount_raw",
        "decimals",
        "user_account",
        "token_account",
        "native",
    )

    mint: str
    amount_raw: int
    decimals: int
    user_account: str
    token_account: str
    native: bool


@dataclass(frozen=True)
class SwapEvent:
    __slots__ = ("inputs", "outputs", "route", "counterparties")

    inputs: Tuple[SwapAsset, ...]
    outputs: Tuple[SwapAsset, ...]
    route: Tuple[str, ...]
    counterparties: Tuple[str, ...]


@dataclass(frozen=True)
class RentAdjustment:
    __slots__ = ("account", "lamports")

    account: str
    lamports: int


@dataclass(frozen=True)
class NormalizedTransaction:
    __slots__ = (
        "signature",
        "slot",
        "block_time",
        "failed",
        "error",
        "source",
        "event_type",
        "fee_payer",
        "fee_lamports",
        "base_fee_lamports",
        "priority_fee_lamports",
        "priority_fee_source",
        "compute_units_consumed",
        "native_transfers",
        "token_transfers",
        "account_balance_deltas",
        "token_balance_deltas",
        "token_fees",
        "rent_adjustments",
        "instructions",
        "swap",
        "account_keys",
    )

    signature: str
    slot: int
    block_time: Optional[int]
    failed: bool
    error: str
    source: str
    event_type: str
    fee_payer: str
    fee_lamports: int
    base_fee_lamports: int
    priority_fee_lamports: int
    priority_fee_source: str
    compute_units_consumed: Optional[int]
    native_transfers: Tuple[NativeTransfer, ...]
    token_transfers: Tuple[TokenTransfer, ...]
    account_balance_deltas: Tuple[AccountBalanceDelta, ...]
    token_balance_deltas: Tuple[TokenBalanceDelta, ...]
    token_fees: Tuple[TokenFee, ...]
    rent_adjustments: Tuple[RentAdjustment, ...]
    instructions: Tuple[ParsedInstruction, ...]
    swap: Optional[SwapEvent]
    account_keys: Tuple[str, ...]
