"""Minimal standard-library Solana JSON-RPC client."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Tuple


class RpcClient:
    """Issue ordered single and batch JSON-RPC requests."""

    def __init__(self, url: str, timeout: float = 20.0) -> None:
        self.url = url
        self.timeout = timeout
        self._request_id = 0
        self._request_id_lock = threading.Lock()

    def _reserve_request_ids(self, count: int = 1) -> List[int]:
        with self._request_id_lock:
            start = self._request_id + 1
            self._request_id += count
        return list(range(start, start + count))

    def _post(self, payload: Any) -> Any:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "SolanaScreener/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError) as exc:
            raise RuntimeError(f"RPC request failed: {exc}") from exc

    def call(self, method: str, params: List[Any]) -> Any:
        request_id = self._reserve_request_ids()[0]
        body = self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        if not isinstance(body, dict):
            raise RuntimeError(f"Invalid {method} RPC response")
        if body.get("error"):
            raise RuntimeError(f"{method}: {body['error']}")
        return body.get("result")

    def get_account_info(self, address: str) -> Dict[str, Any]:
        """Get account info via RPC getAccountInfo call."""
        body = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._reserve_request_ids()[0],
                "method": "getAccountInfo",
                "params": [address, {"encoding": "base64"}],
            }
        )
        if not isinstance(body, dict):
            raise RuntimeError("getAccountInfo: invalid response")
        if body.get("error"):
            raise RuntimeError(f"getAccountInfo: {body['error']}")
        result = body.get("result")
        return result if isinstance(result, dict) else {}

    def batch(self, calls: Iterable[Tuple[str, List[Any]]]) -> List[Any]:
        call_list = list(calls)
        if not call_list:
            return []
        order = self._reserve_request_ids(len(call_list))
        payload = []
        for request_id, (method, params) in zip(order, call_list):
            payload.append(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
        body = self._post(payload)
        if not isinstance(body, list):
            raise RuntimeError("Invalid batch RPC response")
        by_id: Dict[int, Any] = {}
        for item in body:
            if not isinstance(item, dict):
                continue
            response_id = item.get("id")
            if response_id in by_id:
                raise RuntimeError(f"Batch RPC response duplicated id {response_id}")
            by_id[response_id] = item
        results = []
        for request_id in order:
            if request_id not in by_id:
                raise RuntimeError(f"Batch RPC response missing id {request_id}")
            item = by_id[request_id]
            if item.get("error"):
                raise RuntimeError(f"Batch RPC request failed: {item['error']}")
            results.append(item.get("result"))
        return results
