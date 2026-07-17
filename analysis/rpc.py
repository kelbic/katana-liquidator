"""Minimal stdlib JSON-RPC client for Katana public endpoints. READ-ONLY by construction:
only whitelisted eth_* read methods are allowed — no way to send a transaction through this.
The live write-path (sign + broadcast) lives in bot/executor.py behind an explicit flag and a
separate minimal client; this module never signs or sends.

KEEP-ALIVE transport (latency upgrade, 2026-07-17): every call goes over a persistent
http.client connection from a module-level pool (one socket per endpoint; the bot loop is
single-threaded, so no locking). A cold urllib request paid TCP+TLS every call (~115ms vs
~40-60ms warm RTT — measured, ~/.katana-probe) and the hot detect path stacks several calls
back-to-back. A stale socket (LB idle timeout ~60s) is reconnected transparently with exactly
one retry; warm() refreshes the socket with a cheap eth_chainId so the armed fire window
never opens on a cold lane.

Katana mainnet: chainId 747474 (0xb67d2), native ETH, ~1s blocks. RPCs verified 2026-07-13.
"""
from __future__ import annotations

import http.client
import json
import time
import urllib.parse

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


class HttpStatusError(OSError):
    """Non-200 HTTP status from the endpoint (429 rate limit, 5xx LB errors). Transport-class:
    retried/rotated by Rpc.call exactly like the old urllib HTTPError path."""
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


# --- keep-alive connection pool (module-level: shared across Rpc instances — the executor
# builds a fresh Rpc per pass, and the warm socket must survive that) ---------------------
_POOL: dict[str, http.client.HTTPConnection] = {}


def _connect(scheme: str, netloc: str, timeout: float) -> http.client.HTTPConnection:
    """Factory (monkeypatch point for tests)."""
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    return cls(netloc, timeout=timeout)


def _pooled_post(url: str, body: bytes, timeout: float) -> bytes:
    """POST `body` over the kept-alive connection for `url`'s endpoint. Reconnect-on-error
    with exactly ONE retry: a socket left stale by the LB's ~60s idle timeout fails the first
    request; the fresh connection is the retry. Raises HttpStatusError on non-200 (socket kept
    — the body was drained) and the underlying OSError/HTTPException when both attempts fail."""
    u = urllib.parse.urlsplit(url)
    key = f"{u.scheme}://{u.netloc}"
    err: Exception | None = None
    for fresh in (False, True):
        conn = _POOL.get(key)
        if conn is None or fresh:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = _connect(u.scheme, u.netloc, timeout)
            _POOL[key] = conn
        try:
            if conn.sock is not None:
                conn.sock.settimeout(timeout)   # honor the caller's timeout on a reused socket
            conn.request("POST", u.path or "/", body,
                         {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            r = conn.getresponse()
            payload = r.read()
            if r.status != 200:
                raise HttpStatusError(r.status)
            return payload
        except HttpStatusError:
            raise
        except (OSError, http.client.HTTPException) as e:
            err = e
            try:
                conn.close()
            except Exception:
                pass
            _POOL.pop(key, None)
    raise err  # both the pooled socket and the fresh reconnect failed


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

    def _body(self, method: str, params: list) -> bytes:
        self._id += 1
        return json.dumps({"jsonrpc": "2.0", "id": self._id,
                           "method": method, "params": params}).encode()

    def call(self, method: str, params: list):
        if method not in _READ_METHODS:
            raise ValueError(f"method {method} is not in the read-only whitelist")
        body = self._body(method, params)
        last = None
        for attempt in range(self.retries):
            wait = self.min_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
            url = self.urls[attempt % len(self.urls)]
            try:
                d = json.loads(_pooled_post(url, body, self.timeout))
                if "error" in d:
                    err = d["error"]
                    raise RpcError(err.get("code"), err.get("message", ""))
                return d["result"]
            except RpcError:
                raise
            except HttpStatusError as e:
                last = e
                time.sleep(self.backoff_429 * (attempt + 1) if e.status == 429
                           else 0.4 * (attempt + 1))
            except (TimeoutError, json.JSONDecodeError, OSError,
                    http.client.IncompleteRead, http.client.HTTPException) as e:
                last = e
                time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"rpc exhausted retries: {last}")

    # -- keep-alive fire-path helpers (predictive detect, bot/fastpath.py) ----
    def poll_block_number(self, timeout: float = 0.5) -> int | None:
        """Single-shot eth_blockNumber on the keep-alive lane — no pacing, no retry sleeps
        (the tight-poll's next ~18ms tick IS the retry), short timeout so a hung read can't
        stall the armed window. None on any failure; never raises."""
        try:
            d = json.loads(_pooled_post(self.urls[0], self._body("eth_blockNumber", []),
                                        timeout))
            return int(d["result"], 16)
        except Exception:
            return None

    def warm(self, timeout: float = 2.0) -> bool:
        """Open/refresh the primary endpoint's keep-alive socket with a cheap eth_chainId, so
        the next hot call never pays TCP+TLS (~75ms extra, measured). Call before the armed
        window and ~every pass — LB idle timeouts (~60s) must not leave the lane cold.
        Never raises."""
        try:
            _pooled_post(self.urls[0], self._body("eth_chainId", []), timeout)
            return True
        except Exception:
            return False

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
