"""Minimal stdlib JSON-RPC client for Katana public endpoints. READ-ONLY by construction:
only whitelisted eth_* read methods are allowed — no way to send a transaction through this.
The live write-path (sign + broadcast) lives in bot/executor.py behind an explicit flag and a
separate minimal client; this module never signs or sends.

Katana mainnet: chainId 747474 (0xb67d2), native ETH, ~1s blocks. RPCs verified 2026-07-13.
"""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request

# Verified reachable 2026-07-13 (eth_chainId -> 0xb67d2). Keep head/logs/reaction on
# different endpoints in the executor to avoid single-endpoint rate limits.
DEFAULT_RPCS = [
    "https://rpc.katana.network",
    "https://rpc.katanarpc.com",
    "https://katana.gateway.tenderly.co",
]

CHAIN_ID = 747474

_READ_METHODS = {
    "eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_getLogs",
    "eth_call", "eth_getCode", "eth_getTransactionReceipt", "eth_getTransactionByHash",
    "eth_getBalance", "eth_getStorageAt", "eth_gasPrice", "eth_maxPriorityFeePerGas",
}


class RpcError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"rpc error {code}: {message}")
        self.code = code
        self.message = message


class Rpc:
    def __init__(self, urls: list[str] | None = None, timeout: float = 25.0, retries: int = 6,
                 min_interval: float = 0.05, backoff_429: float = 1.0):
        self.urls = list(urls or DEFAULT_RPCS)
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval  # gentle pacing — public endpoints rate-limit bursts
        self.backoff_429 = backoff_429    # race paths pass a small value: rotate, don't wait
        self._id = 0
        self._last_call = 0.0

    def call(self, method: str, params: list):
        if method not in _READ_METHODS:
            raise ValueError(f"method {method} is not in the read-only whitelist")
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id,
                           "method": method, "params": params}).encode()
        last = None
        for attempt in range(self.retries):
            wait = self.min_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
            url = self.urls[attempt % len(self.urls)]
            req = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = json.loads(r.read())
                if "error" in d:
                    err = d["error"]
                    raise RpcError(err.get("code"), err.get("message", ""))
                return d["result"]
            except RpcError:
                raise
            except urllib.error.HTTPError as e:
                last = e
                time.sleep(self.backoff_429 * (attempt + 1) if e.code == 429
                           else 0.4 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError,
                    http.client.IncompleteRead, http.client.HTTPException) as e:
                last = e
                time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"rpc exhausted retries: {last}")

    # -- convenience wrappers --------------------------------------------
    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def get_block(self, number: int | str, full: bool = False) -> dict:
        tag = number if isinstance(number, str) else hex(number)
        return self.call("eth_getBlockByNumber", [tag, full])

    def get_logs(self, address, topics, from_block: int, to_block: int) -> list:
        return self.call("eth_getLogs", [{
            "address": address, "topics": topics,
            "fromBlock": hex(from_block), "toBlock": hex(to_block)}])

    def get_code(self, address: str, tag: str = "latest") -> str:
        return self.call("eth_getCode", [address, tag])

    def eth_call(self, to: str, data: str, tag: str = "latest", gas: int | None = None) -> str:
        req = {"to": to, "data": data}
        if gas is not None:
            req["gas"] = hex(gas)
        return self.call("eth_call", [req, tag])

    def gas_price(self) -> int:
        return int(self.call("eth_gasPrice", []), 16)

    def receipt(self, tx_hash: str) -> dict:
        return self.call("eth_getTransactionReceipt", [tx_hash])


def get_logs_chunked(rpc: Rpc, address, topics, from_block: int, to_block: int,
                     chunk: int = 100_000, on_progress=None) -> list:
    """getLogs over a big range in fixed windows; halves the window on limit errors."""
    out = []
    lo = from_block
    while lo <= to_block:
        hi = min(lo + chunk - 1, to_block)
        try:
            logs = rpc.get_logs(address, topics, lo, hi)
        except (RpcError, http.client.IncompleteRead, RuntimeError):
            # RpcError = explicit "range too large"; IncompleteRead/RuntimeError = the public
            # Katana RPC truncated a big chunked response (or call() exhausted retries on it).
            # In every case, retry a smaller window before giving up.
            if hi > lo:
                chunk = max(1000, chunk // 2)
                continue
            raise
        out.extend(logs)
        if on_progress:
            on_progress(hi, to_block, len(out))
        lo = hi + 1
    return out
