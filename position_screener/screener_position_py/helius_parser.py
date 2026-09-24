"""Single-pass normalization for Helius Enhanced, Parsed Event, and Raw payloads."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional, Tuple, Union

from core.constants import (  # noqa: F401  (LAMPORTS_PER_SOL re-exported)
    LAMPORTS_PER_SOL,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)

from .coercion import (
    as_items,
    as_mapping,
    b58decode,
    to_int,
    token_amount,
    ui_amount_to_raw,
)
from .helius_types import (
    AccountBalanceDelta,
    NamedAccount,
    NativeTransfer,
    NormalizedTransaction,
    ParsedInstruction,
    RentAdjustment,
    SwapAsset,
    SwapEvent,
    TokenBalanceDelta,
    TokenFee,
    TokenTransfer,
)

TOKEN_PROGRAM = TOKEN_PROGRAM_ID
TOKEN_2022_PROGRAM = TOKEN_2022_PROGRAM_ID
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
SYSTEM_PROGRAM = "11111111111111111111111111111111"

DEX_PROGRAMS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "JUPITER",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": "JUPITER",
    "675kPX9MHTjS2zt1qfr1NYHuzeLk9YjFNHhGPNi4iYpH": "RAYDIUM_AMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "RAYDIUM_CLMM",
    "CPMMoo8L3F4NbTegBCKVN9W2KaxMPxQkcVkc1q7c5UG": "RAYDIUM_CPMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "ORCA",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "METEORA",
    "6EF8rrecthR5DkzonD7a2yXJkTjArYwbNQXJr5nW6P": "PUMP_FUN",
}

JsonPayload = Union[bytes, str, Mapping[str, Any], Sequence[Mapping[str, Any]]]


class HeliusWebhookParser:
    """Sanitize a webhook body once and emit compact immutable transactions."""

    __slots__ = ()

    def parse_payload(self, payload: JsonPayload) -> Tuple[NormalizedTransaction, ...]:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(payload, Mapping):
            records = (payload,)
        elif isinstance(payload, Sequence):
            records = tuple(item for item in payload if isinstance(item, Mapping))
        else:
            raise ValueError("Helius payload must be a transaction object or array")
        return tuple(self.parse_transaction(record) for record in records)

    def parse_transaction(self, record: Mapping[str, Any]) -> NormalizedTransaction:
        parsed = as_mapping(record.get("parsed"))
        body = parsed or record
        raw_transaction = as_mapping(record.get("transaction"))
        message = as_mapping(raw_transaction.get("message"))
        meta = as_mapping(record.get("meta"))
        account_keys = self._account_keys(message, meta)
        instructions = self._instructions(body, message, meta, account_keys)

        signatures = tuple(
            str(item) for item in as_items(raw_transaction.get("signatures")) if item
        )
        signature = str(
            record.get("signature")
            or body.get("signature")
            or (signatures[0] if signatures else "")
        )
        error_value = body.get("transactionError")
        if error_value is None:
            error_value = meta.get("err")
        status = str(body.get("transactionStatus", "")).upper()
        failed = error_value is not None or status in {"ERROR", "FAILED"}
        error = (
            ""
            if error_value is None
            else json.dumps(error_value, sort_keys=True, default=str)
        )

        fee = to_int(body.get("fee", meta.get("fee")))
        signature_count = len(signatures) or to_int(
            as_mapping(message.get("header")).get("numRequiredSignatures"), 1
        )
        base_fee = min(fee, max(signature_count, 1) * 5_000) if fee else 0
        priority_value = body.get("priorityFee")
        if priority_value is None:
            priority_value = meta.get("prioritizationFee", meta.get("priorityFee"))
        if priority_value is not None:
            priority_fee = max(to_int(priority_value), 0)
            priority_source = "payload"
        elif fee:
            priority_fee = max(fee - base_fee, 0)
            priority_source = "fee_minus_signature_base"
        else:
            priority_fee = 0
            priority_source = "unavailable"

        native_transfers = self._native_transfers(body, instructions, account_keys)
        token_transfers = self._token_transfers(body, instructions)
        account_deltas = self._account_deltas(body, meta, account_keys)
        token_deltas = self._token_deltas(body, meta, account_keys)
        token_fees = self._token_fees(body, instructions, token_transfers)
        rent = tuple(
            RentAdjustment(
                str(as_mapping(item).get("pubkey", "")),
                to_int(as_mapping(item).get("lamports")),
            )
            for item in as_items(meta.get("rewards"))
            if str(as_mapping(item).get("rewardType", "")).lower() == "rent"
        )
        fee_payer = str(
            body.get("feePayer") or (account_keys[0] if account_keys else "")
        )
        swap = self._swap(
            body, instructions, native_transfers, token_transfers, fee_payer
        )

        return NormalizedTransaction(
            signature=signature,
            slot=to_int(body.get("slot", record.get("slot"))),
            block_time=self._optional_int(
                body.get("blockTime", body.get("timestamp", record.get("blockTime")))
            ),
            failed=failed,
            error=error,
            source=str(body.get("source", "")),
            event_type=str(body.get("type", "")),
            fee_payer=fee_payer,
            fee_lamports=fee,
            base_fee_lamports=base_fee,
            priority_fee_lamports=priority_fee,
            priority_fee_source=priority_source,
            compute_units_consumed=self._optional_int(
                meta.get("computeUnitsConsumed", body.get("computeUnitsConsumed"))
            ),
            native_transfers=native_transfers,
            token_transfers=token_transfers,
            account_balance_deltas=account_deltas,
            token_balance_deltas=token_deltas,
            token_fees=token_fees,
            rent_adjustments=rent,
            instructions=instructions,
            swap=swap,
            account_keys=account_keys,
        )

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        return None if value is None else to_int(value)

    @staticmethod
    def _account_keys(
        message: Mapping[str, Any], meta: Mapping[str, Any]
    ) -> Tuple[str, ...]:
        keys = []
        for item in as_items(message.get("accountKeys")):
            value = as_mapping(item).get("pubkey") if isinstance(item, Mapping) else item
            if value:
                keys.append(str(value))
        loaded = as_mapping(meta.get("loadedAddresses"))
        keys.extend(str(item) for item in as_items(loaded.get("writable")))
        keys.extend(str(item) for item in as_items(loaded.get("readonly")))
        return tuple(keys)

    def _instructions(
        self,
        body: Mapping[str, Any],
        message: Mapping[str, Any],
        meta: Mapping[str, Any],
        keys: Tuple[str, ...],
    ) -> Tuple[ParsedInstruction, ...]:
        result: List[ParsedInstruction] = []
        outer = (
            body.get("instructions")
            if body.get("instructions") is not None
            else message.get("instructions")
        )
        for item in as_items(outer):
            instruction = self._instruction(as_mapping(item), keys, False)
            result.append(instruction)
            for inner in as_items(as_mapping(item).get("innerInstructions")):
                result.append(self._instruction(as_mapping(inner), keys, True))
        for group in as_items(meta.get("innerInstructions")):
            for item in as_items(as_mapping(group).get("instructions")):
                result.append(self._instruction(as_mapping(item), keys, True))
        return tuple(result)

    @staticmethod
    def _instruction(
        item: Mapping[str, Any], keys: Tuple[str, ...], inner: bool
    ) -> ParsedInstruction:
        program_id = str(item.get("programId", ""))
        if not program_id:
            index = to_int(item.get("programIdIndex"), -1)
            program_id = keys[index] if 0 <= index < len(keys) else ""
        parsed = as_mapping(item.get("parsed"))
        decoded = as_mapping(item.get("decoded"))
        name = str(
            item.get("instructionName")
            or parsed.get("type")
            or decoded.get("name")
            or ""
        )
        accounts: List[NamedAccount] = []
        decoded_accounts = decoded.get("accounts")
        if isinstance(decoded_accounts, (list, tuple)):
            for account in decoded_accounts:
                value = as_mapping(account)
                address = value.get("pubkey") or value.get("address")
                if address:
                    accounts.append(
                        NamedAccount(str(value.get("name", "")), str(address))
                    )
        elif isinstance(item.get("accounts"), (list, tuple)):
            for index, account in enumerate(item["accounts"]):
                address = (
                    keys[account]
                    if isinstance(account, int) and 0 <= account < len(keys)
                    else str(account)
                )
                accounts.append(NamedAccount(str(index), address))
        for key, value in as_mapping(parsed.get("info")).items():
            if isinstance(value, str) and len(value) >= 32:
                accounts.append(NamedAccount(str(key), value))
        info = as_mapping(parsed.get("info"))
        token_amount_obj = as_mapping(info.get("tokenAmount"))
        if token_amount_obj:
            amount_raw, decimals = token_amount(token_amount_obj)
        else:
            amount_raw = to_int(info.get("lamports", info.get("amount")))
            decimals = to_int(info.get("decimals"))
        data = str(item.get("data", ""))
        raw = b58decode(data)
        if (
            not amount_raw
            and program_id in {TOKEN_PROGRAM, TOKEN_2022_PROGRAM}
            and len(raw) >= 9
        ):
            amount_raw = int.from_bytes(raw[1:9], "little")
            decimals = raw[9] if len(raw) > 9 and raw[0] in {12, 13} else decimals
        return ParsedInstruction(
            program_id, name, tuple(accounts), data, amount_raw, decimals, inner
        )

    @staticmethod
    def _native_transfers(
        body: Mapping[str, Any],
        instructions: Tuple[ParsedInstruction, ...],
        keys: Tuple[str, ...],
    ) -> Tuple[NativeTransfer, ...]:
        explicit = body.get("nativeTransfers")
        if isinstance(explicit, (list, tuple)):
            return tuple(
                NativeTransfer(
                    str(as_mapping(item).get("fromUserAccount", "")),
                    str(as_mapping(item).get("toUserAccount", "")),
                    to_int(as_mapping(item).get("amount")),
                )
                for item in explicit
            )
        result = []
        for instruction in instructions:
            names = {
                account.name.lower(): account.address
                for account in instruction.accounts
            }
            if (
                instruction.program_id == SYSTEM_PROGRAM
                and instruction.name.lower() in {"transfer", "transferwithseed"}
            ):
                source = names.get("source", names.get("0", ""))
                destination = names.get("destination", names.get("1", ""))
                # Parsed System Program instructions expose lamports in info,
                # while compiled instructions retain it in little-endian data.
                raw = b58decode(instruction.data)
                lamports = instruction.amount_raw or (
                    int.from_bytes(raw[4:12], "little") if len(raw) >= 12 else 0
                )
                if source and destination and lamports:
                    result.append(NativeTransfer(source, destination, lamports))
        return tuple(result)

    @staticmethod
    def _token_transfers(
        body: Mapping[str, Any], instructions: Tuple[ParsedInstruction, ...]
    ) -> Tuple[TokenTransfer, ...]:
        explicit = body.get("tokenTransfers")
        if isinstance(explicit, (list, tuple)):
            result = []
            for item in explicit:
                value = as_mapping(item)
                raw_amount = value.get("rawTokenAmount")
                if raw_amount is not None:
                    amount, decimals = token_amount(raw_amount)
                else:
                    decimals = to_int(value.get("decimals"))
                    amount = ui_amount_to_raw(value.get("tokenAmount", 0), decimals)
                result.append(
                    TokenTransfer(
                        str(value.get("mint", "")),
                        str(value.get("fromUserAccount", "")),
                        str(value.get("toUserAccount", "")),
                        str(value.get("fromTokenAccount", "")),
                        str(value.get("toTokenAccount", "")),
                        amount,
                        decimals,
                        str(value.get("tokenProgram", value.get("programId", ""))),
                    )
                )
            return tuple(result)
        result = []
        for instruction in instructions:
            if instruction.program_id not in {TOKEN_PROGRAM, TOKEN_2022_PROGRAM}:
                continue
            if instruction.name.lower() not in {
                "transfer",
                "transferchecked",
                "transfercheckedwithfee",
            }:
                continue
            names = {
                account.name.lower(): account.address
                for account in instruction.accounts
            }
            result.append(
                TokenTransfer(
                    names.get("mint", ""),
                    names.get("authority", names.get("owner", "")),
                    "",
                    names.get("source", names.get("0", "")),
                    names.get("destination", names.get("1", "")),
                    instruction.amount_raw,
                    instruction.decimals,
                    instruction.program_id,
                )
            )
        return tuple(result)

    @staticmethod
    def _account_deltas(
        body: Mapping[str, Any], meta: Mapping[str, Any], keys: Tuple[str, ...]
    ) -> Tuple[AccountBalanceDelta, ...]:
        account_data = body.get("accountData")
        if isinstance(account_data, (list, tuple)):
            return tuple(
                AccountBalanceDelta(
                    str(as_mapping(item).get("account", "")),
                    0,
                    0,
                    to_int(as_mapping(item).get("nativeBalanceChange")),
                )
                for item in account_data
            )
        pre = tuple(to_int(item) for item in as_items(meta.get("preBalances")))
        post = tuple(to_int(item) for item in as_items(meta.get("postBalances")))
        return tuple(
            AccountBalanceDelta(
                keys[index] if index < len(keys) else "",
                before,
                post[index],
                post[index] - before,
            )
            for index, before in enumerate(pre)
            if index < len(post)
        )

    @staticmethod
    def _token_deltas(
        body: Mapping[str, Any], meta: Mapping[str, Any], keys: Tuple[str, ...]
    ) -> Tuple[TokenBalanceDelta, ...]:
        account_data = body.get("accountData")
        if isinstance(account_data, (list, tuple)):
            result = []
            for account in account_data:
                account_value = as_mapping(account)
                for item in as_items(account_value.get("tokenBalanceChanges")):
                    value = as_mapping(item)
                    amount, decimals = token_amount(value.get("rawTokenAmount"))
                    result.append(
                        TokenBalanceDelta(
                            str(
                                value.get(
                                    "tokenAccount", account_value.get("account", "")
                                )
                            ),
                            -1,
                            str(value.get("userAccount", "")),
                            str(value.get("mint", "")),
                            amount,
                            decimals,
                            str(value.get("programId", "")),
                        )
                    )
            return tuple(result)
        balances: Dict[Tuple[int, str], List[Any]] = {}
        for side, field in ((0, "preTokenBalances"), (1, "postTokenBalances")):
            for item in as_items(meta.get(field)):
                value = as_mapping(item)
                index = to_int(value.get("accountIndex"), -1)
                if index < 0:
                    continue
                key = (index, str(value.get("mint", "")))
                entry = balances.setdefault(key, [0, 0, 0, "", ""])
                amount, decimals = token_amount(value.get("uiTokenAmount"))
                entry[side] = amount
                entry[2] = decimals
                entry[3] = str(value.get("owner", entry[3]))
                entry[4] = str(value.get("programId", entry[4]))
        return tuple(
            TokenBalanceDelta(
                keys[index] if 0 <= index < len(keys) else "",
                index,
                values[3],
                mint,
                values[1] - values[0],
                values[2],
                values[4],
            )
            for (index, mint), values in balances.items()
        )

    @staticmethod
    def _token_fees(
        body: Mapping[str, Any],
        instructions: Tuple[ParsedInstruction, ...],
        transfers: Tuple[TokenTransfer, ...],
    ) -> Tuple[TokenFee, ...]:
        result = []
        for index, item in enumerate(as_items(body.get("tokenTransfers"))):
            value = as_mapping(item)
            fee_value = value.get("feeAmount", value.get("transferFee"))
            if fee_value is not None:
                fee, decimals = (
                    token_amount(fee_value)
                    if isinstance(fee_value, Mapping)
                    else (to_int(fee_value), transfers[index].decimals)
                )
                transfer = transfers[index]
                result.append(
                    TokenFee(
                        transfer.mint,
                        fee,
                        decimals,
                        transfer.token_program or TOKEN_2022_PROGRAM,
                        "token_transfer",
                    )
                )
        for instruction in instructions:
            if (
                instruction.program_id == TOKEN_2022_PROGRAM
                and "fee" in instruction.name.lower()
            ):
                names = {
                    account.name.lower(): account.address
                    for account in instruction.accounts
                }
                result.append(
                    TokenFee(
                        names.get("mint", ""),
                        0,
                        0,
                        TOKEN_2022_PROGRAM,
                        instruction.name,
                    )
                )
        return tuple(result)

    def _swap(
        self,
        body: Mapping[str, Any],
        instructions: Tuple[ParsedInstruction, ...],
        native_transfers: Tuple[NativeTransfer, ...],
        token_transfers: Tuple[TokenTransfer, ...],
        fee_payer: str,
    ) -> Optional[SwapEvent]:
        swap = as_mapping(as_mapping(body.get("events")).get("swap"))
        if not swap and str(body.get("type", "")).upper() != "SWAP":
            return None
        inputs = self._swap_assets(swap, "tokenInputs", "nativeInput")
        outputs = self._swap_assets(swap, "tokenOutputs", "nativeOutput")
        route: List[str] = []
        source = str(body.get("source", ""))
        if source:
            route.append(source.upper())
        for instruction in instructions:
            dex = DEX_PROGRAMS.get(instruction.program_id)
            if dex and dex not in route:
                route.append(dex)
        for inner in as_items(swap.get("innerSwaps")):
            program = str(
                as_mapping(inner).get("programInfo", as_mapping(inner).get("source", ""))
            )
            if program and program not in route:
                route.append(program)
        counterparties = set()
        for transfer in native_transfers:
            if transfer.from_account == fee_payer and transfer.to_account:
                counterparties.add(transfer.to_account)
            elif transfer.to_account == fee_payer and transfer.from_account:
                counterparties.add(transfer.from_account)
        for transfer in token_transfers:
            if transfer.from_user_account == fee_payer and transfer.to_user_account:
                counterparties.add(transfer.to_user_account)
            elif transfer.to_user_account == fee_payer and transfer.from_user_account:
                counterparties.add(transfer.from_user_account)
        return SwapEvent(inputs, outputs, tuple(route), tuple(sorted(counterparties)))

    @staticmethod
    def _swap_assets(
        swap: Mapping[str, Any], token_field: str, native_field: str
    ) -> Tuple[SwapAsset, ...]:
        result = []
        native = as_mapping(swap.get(native_field))
        if native:
            result.append(
                SwapAsset(
                    "SOL",
                    to_int(native.get("amount")),
                    9,
                    str(native.get("account", "")),
                    "",
                    True,
                )
            )
        for item in as_items(swap.get(token_field)):
            value = as_mapping(item)
            raw = value.get("rawTokenAmount")
            if raw is not None:
                amount, decimals = token_amount(raw)
            else:
                decimals = to_int(value.get("decimals"))
                amount = ui_amount_to_raw(value.get("tokenAmount", 0), decimals)
            result.append(
                SwapAsset(
                    str(value.get("mint", "")),
                    amount,
                    decimals,
                    str(value.get("userAccount", "")),
                    str(value.get("tokenAccount", "")),
                    False,
                )
            )
        return tuple(result)
