"""Mempool WSS subscription manager for the same-block backrun layer (additive to the v3 fast
path; behind KT_MEMPOOL, default-SHADOW).

The Conduit op-reth node (wss://rpc.katanarpc.com) — atypically for an OP stack — exposes a
PUBLIC mempool and eth_subscribe. This module runs ONE background thread holding a persistent
websocket, subscribing to:
  * newHeads               — block arrivals 36-86ms after the block ts (vs our ~0.3s poll);
  * newPendingTransactions — pending tx hashes (or full bodies) BEFORE they land.
For each pending tx it fingerprints `to`/`from` against bot.oracles (our 6 markets' Chainlink
aggregators + transmitter EOAs). A match means an oracle push is about to reprice a KNOWN
market — the same-block backrun's trigger, with the push's exact priority fee attached.

STRICT THREADING CONTRACT: the single-threaded main loop NEVER blocks on the socket. This
thread owns its own transport (the WSS) and its own read connection (a dedicated http.client
for tx-body fetches) — it never touches analysis.rpc's keep-alive pool or the executor's write
lane. It communicates with the main loop only through (a) a lock-guarded latest-head snapshot
the loop may read, and (b) an on_signal callback the loop registers. If the socket drops it
logs LOUDLY, marks itself unhealthy, and reconnects with capped backoff — the existing
predictive poll keeps firing next-block regardless (this layer is purely additive), so a dead
WSS degrades to 'no same-block attempts', never a wedge or busy-loop.

The transport and the tx fetcher are INJECTABLE so the reconnect/dispatch/detection logic is
unit-tested with zero network (test_mempool.py). Importing this module opens nothing.
"""
from __future__ import annotations

import http.client
import json
import os
import struct
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from bot import oracles

WSS_URL = os.environ.get("KT_MEMPOOL_WSS_URL", "wss://rpc.katanarpc.com")
HTTP_URL = os.environ.get("KT_MEMPOOL_HTTP_URL", "https://rpc.katanarpc.com")
# newHeads arrive ~1/s; this much silence means the stream is wedged -> unhealthy -> reconnect.
STALE_SEC = float(os.environ.get("KT_MEMPOOL_STALE_SEC", "5.0"))
BACKOFF_MAX_SEC = float(os.environ.get("KT_MEMPOOL_BACKOFF_MAX", "30.0"))
SEEN_MAX = int(os.environ.get("KT_MEMPOOL_SEEN_MAX", "4096"))
# resolve 'did the oracle tx land, and in which block' within this many heads, then drop.
RESOLVE_HEADS = int(os.environ.get("KT_MEMPOOL_RESOLVE_HEADS", "4"))


# --- websocket framing (RFC 6455 subset; pure — unit-tested) --------------------------------
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def encode_frame(payload: bytes, opcode: int = OP_TEXT, mask: bool = True) -> bytes:
    """One FIN frame. Client->server frames MUST be masked (mask=True)."""
    out = bytearray([0x80 | opcode])
    ln = len(payload)
    flag = 0x80 if mask else 0
    if ln < 126:
        out.append(flag | ln)
    elif ln < 65536:
        out.append(flag | 126)
        out += struct.pack(">H", ln)
    else:
        out.append(flag | 127)
        out += struct.pack(">Q", ln)
    if mask:
        m = os.urandom(4)
        out += m
        out += bytes(c ^ m[i % 4] for i, c in enumerate(payload))
    else:
        out += payload
    return bytes(out)


def read_frame(recv_exactly):
    """Read ONE frame using recv_exactly(n)->bytes (which must return exactly n bytes or raise).
    Returns (fin: bool, opcode: int, data: bytes). Handles the (unmasked) server framing and,
    defensively, masked frames."""
    h = recv_exactly(2)
    fin = bool(h[0] & 0x80)
    opcode = h[0] & 0x0F
    masked = bool(h[1] & 0x80)
    ln = h[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", recv_exactly(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", recv_exactly(8))[0]
    mask = recv_exactly(4) if masked else None
    data = recv_exactly(ln) if ln else b""
    if mask:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    return fin, opcode, data


class WsClosed(Exception):
    """The peer closed the websocket (or the socket died) — the manager reconnects."""


# --- real TLS websocket transport (thin adapter; framing above is what's tested) ------------
class WsConn:
    """Persistent TLS websocket to a JSON-RPC endpoint. Reassembles fragmented messages and
    answers pings so a long-lived connection is not dropped. send_text/recv_message/close is
    the surface the manager (and its fakes) use."""

    def __init__(self, url: str, timeout: float = 10.0):
        import socket
        import ssl
        import base64
        u = urllib.parse.urlsplit(url)
        host = u.hostname
        port = u.port or (443 if u.scheme in ("wss", "https") else 80)
        path = u.path or "/"
        raw = socket.create_connection((host, port), timeout=timeout)
        if u.scheme in ("wss", "https"):
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\n\r\n")
        raw.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            c = raw.recv(1)
            if not c:
                raise WsClosed("handshake closed")
            resp += c
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise WsClosed(f"handshake not upgraded: {resp[:80]!r}")
        self._sock = raw
        self.timeout = timeout

    def _recv_exactly(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            c = self._sock.recv(n - len(out))
            if not c:
                raise WsClosed("socket closed")
            out += c
        return out

    def send_text(self, s: str) -> None:
        self._sock.sendall(encode_frame(s.encode(), OP_TEXT, mask=True))

    def recv_message(self, timeout: float | None = None) -> str:
        """Next complete text message. Answers pings, skips pongs, raises WsClosed on close.
        Reassembles continuation frames (full-tx pending bodies can fragment)."""
        self._sock.settimeout(self.timeout if timeout is None else timeout)
        buf = bytearray()
        while True:
            fin, opcode, data = read_frame(self._recv_exactly)
            if opcode == OP_CLOSE:
                raise WsClosed("peer close frame")
            if opcode == OP_PING:
                self._sock.sendall(encode_frame(data, OP_PONG, mask=True))
                continue
            if opcode == OP_PONG:
                continue
            buf += data                       # TEXT/BIN or CONT
            if fin:
                return buf.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


# --- dedicated tx-body fetcher (separate from analysis.rpc's shared pool) --------------------
class _TxFetcher:
    """Kept-alive http.client for eth_getTransactionByHash — the WSS thread's OWN read lane, so
    a pending-body fetch never contends with the main loop's keep-alive pool. Reconnect-once."""

    def __init__(self, url: str, timeout: float = 3.0):
        self._u = urllib.parse.urlsplit(url)
        self.timeout = timeout
        self._conn: http.client.HTTPConnection | None = None
        self._id = 0

    def __call__(self, tx_hash: str) -> dict | None:
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id,
                           "method": "eth_getTransactionByHash", "params": [tx_hash]}).encode()
        for fresh in (False, True):
            if self._conn is None or fresh:
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                cls = (http.client.HTTPSConnection if self._u.scheme == "https"
                       else http.client.HTTPConnection)
                self._conn = cls(self._u.netloc, timeout=self.timeout)
            try:
                self._conn.request("POST", self._u.path or "/", body,
                                   {"Content-Type": "application/json",
                                    "User-Agent": "Mozilla/5.0"})
                d = json.loads(self._conn.getresponse().read())
                return d.get("result")            # None if not yet propagated / dropped
            except (OSError, http.client.HTTPException, ValueError):
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
        return None


# --- oracle signal handed to the executor callback ------------------------------------------
@dataclass
class OracleSignal:
    tx_hash: str
    frm: str | None
    to: str | None
    market_ids: set[str]
    tip_wei: int | None
    detect_mono: float          # monotonic instant the WSS notification was received
    detect_wall: float          # wall-clock (for the shadow log / analyzer)
    head_block: int | None      # latest head at detect (block boundary phase reference)
    head_age_ms: float | None   # ms since that head arrived (position within the block)
    landed_block: int | None = field(default=None)   # filled by resolution


# --- the manager ----------------------------------------------------------------------------
class MempoolClient:
    """Background WSS subscription manager. Construct with an on_signal callback; call start().
    on_signal(OracleSignal) fires in THIS thread on every detected oracle push. on_resolve(sig)
    fires once the push lands (landed_block set) or is dropped (landed_block stays None). Both
    run in the WSS thread — keep them fast and thread-safe (the executor guards its state)."""

    def __init__(self, on_signal, on_resolve=None, fetch_tx=None, connect=None,
                 wss_url: str = WSS_URL, http_url: str = HTTP_URL,
                 now=time.monotonic, sleep=time.sleep, log=print,
                 stale_sec: float = STALE_SEC, backoff_max: float = BACKOFF_MAX_SEC):
        self.on_signal = on_signal
        self.on_resolve = on_resolve
        self._connect = connect or (lambda: WsConn(wss_url))
        self._fetch_tx = fetch_tx if fetch_tx is not None else _TxFetcher(http_url)
        self._now, self._sleep, self._log = now, sleep, log
        self.stale_sec, self.backoff_max = stale_sec, backoff_max
        self._lock = threading.Lock()
        self._head: dict | None = None          # {block, ts, arrival_mono, arrival_wall}
        self._connected = False
        self._last_msg = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sub_head = None                    # subscription ids (set on ack / _subscribe)
        self._sub_pending = None
        self._seen: dict[str, None] = {}        # bounded dedup of pending hashes (insertion ord)
        self._resolving: list[dict] = []        # unresolved signals awaiting a landed block
        self.stats = {"reconnects": 0, "heads": 0, "pending": 0, "oracle_hits": 0,
                      "fetch_fail": 0}

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="mempool-wss", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._connected = False

    # -- main loop reads these (thread-safe) ------------------------------------
    def latest_head(self) -> dict | None:
        with self._lock:
            return dict(self._head) if self._head else None

    def healthy(self) -> bool:
        """Connected AND a message within stale_sec — the main loop uses this only for telemetry;
        the same-block layer simply goes quiet when unhealthy (predictive poll is unaffected)."""
        with self._lock:
            return self._connected and (self._now() - self._last_msg) < self.stale_sec

    # -- background thread ------------------------------------------------------
    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            conn = None
            try:
                conn = self._connect()
                self._subscribe(conn)
                with self._lock:
                    self._connected = True
                    self._last_msg = self._now()
                self._log("[mempool] WSS connected + subscribed (newHeads, "
                          "newPendingTransactions)")
                backoff = 0.5
                self._serve(conn)
            except Exception as e:                # WsClosed, socket errors, dispatch bugs
                self._log(f"[mempool] WSS dropped ({type(e).__name__}: {str(e)[:120]}) — "
                          f"reconnecting in {backoff:.1f}s; predictive poll unaffected")
            finally:
                with self._lock:
                    self._connected = False
                if conn is not None:
                    conn.close()
            if self._stop.is_set():
                break
            self.stats["reconnects"] += 1
            self._sleep(backoff)
            backoff = min(self.backoff_max, backoff * 2)   # capped exponential — never busy-loop

    def _subscribe(self, conn) -> None:
        self._sub_head = None
        self._sub_pending = None
        conn.send_text(json.dumps({"jsonrpc": "2.0", "id": 1,
                                   "method": "eth_subscribe", "params": ["newHeads"]}))
        # ask for FULL bodies (op-reth supports the boolean); we fall back to a fetch if the
        # node streams bare hashes instead.
        conn.send_text(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "eth_subscribe",
                                   "params": ["newPendingTransactions", True]}))

    def _serve(self, conn) -> None:
        while not self._stop.is_set():
            msg = conn.recv_message()
            with self._lock:
                self._last_msg = self._now()
            try:
                j = json.loads(msg)
            except ValueError:
                continue
            if "id" in j and "result" in j:                 # subscription ack
                if j["id"] == 1:
                    self._sub_head = j["result"]
                elif j["id"] == 2:
                    self._sub_pending = j["result"]
                continue
            if j.get("method") != "eth_subscription":
                continue
            params = j.get("params") or {}
            sub = params.get("subscription")
            result = params.get("result")
            if sub == self._sub_head:
                self._on_head(result)
            elif sub == self._sub_pending:
                self._on_pending(result)

    # -- newHeads ---------------------------------------------------------------
    def _on_head(self, head: dict) -> None:
        try:
            block = int(head["number"], 16)
            ts = int(head["timestamp"], 16)
        except (KeyError, TypeError, ValueError):
            return
        snap = {"block": block, "ts": ts, "arrival_mono": self._now(),
                "arrival_wall": time.time()}
        with self._lock:
            self._head = snap
        self.stats["heads"] += 1
        self._resolve_pending(block)

    # -- newPendingTransactions -------------------------------------------------
    def _on_pending(self, result) -> None:
        self.stats["pending"] += 1
        tx = None
        tx_hash = None
        if isinstance(result, dict):                        # full-body stream (preferred)
            tx = result
            tx_hash = (tx.get("hash") or "").lower()
        elif isinstance(result, str):                       # hash-only stream -> fetch body
            tx_hash = result.lower()
        else:
            return
        if not tx_hash or tx_hash in self._seen:
            return
        self._remember(tx_hash)
        if tx is None:
            try:
                tx = self._fetch_tx(tx_hash)
            except Exception:
                tx = None
            if tx is None:
                self.stats["fetch_fail"] += 1
                return
        to, frm = (tx.get("to") or None), (tx.get("from") or None)
        if not oracles.is_oracle_tx(to, frm):
            return
        market_ids = oracles.markets_for_tx(to, frm)
        if not market_ids:
            return
        self.stats["oracle_hits"] += 1
        with self._lock:
            head = dict(self._head) if self._head else None
        # base fee isn't in the head payload and is pinned ~0.001 gwei on Katana, so the tip is
        # taken directly as maxPriorityFeePerGas (what the committee set) — the value op-reth
        # orders by and the same-block backrun matches to the wei.
        detect_mono = self._now()
        sig = OracleSignal(
            tx_hash=tx_hash, frm=frm, to=to, market_ids=market_ids,
            tip_wei=oracles.tx_priority_fee_wei(tx),
            detect_mono=detect_mono, detect_wall=time.time(),
            head_block=(head or {}).get("block"),
            head_age_ms=(None if head is None
                         else (detect_mono - head["arrival_mono"]) * 1000.0))
        self._resolving.append({"sig": sig, "detect_head": sig.head_block, "heads_left":
                                RESOLVE_HEADS})
        try:
            self.on_signal(sig)
        except Exception as e:
            self._log(f"[mempool] on_signal error: {str(e)[:160]}")

    # -- shadow landing resolution ---------------------------------------------
    def _resolve_pending(self, head_block: int) -> None:
        """On each newHead, try to resolve where the still-unresolved oracle txs LANDED, so the
        shadow analyzer can correlate our would-send timing against the real inclusion block."""
        if not self._resolving:
            return
        still: list[dict] = []
        for rec in self._resolving:
            sig = rec["sig"]
            landed = None
            try:
                tx = self._fetch_tx(sig.tx_hash)
                if tx and tx.get("blockNumber"):
                    landed = int(tx["blockNumber"], 16)
            except Exception:
                landed = None
            rec["heads_left"] -= 1
            if landed is not None:
                sig.landed_block = landed
                if self.on_resolve:
                    try:
                        self.on_resolve(sig)
                    except Exception as e:
                        self._log(f"[mempool] on_resolve error: {str(e)[:120]}")
            elif rec["heads_left"] > 0:
                still.append(rec)
            elif self.on_resolve:                           # gave up — report unresolved (drop)
                try:
                    self.on_resolve(sig)
                except Exception as e:
                    self._log(f"[mempool] on_resolve error: {str(e)[:120]}")
        self._resolving = still

    def _remember(self, tx_hash: str) -> None:
        self._seen[tx_hash] = None
        if len(self._seen) > SEEN_MAX:                      # bounded FIFO dedup
            for k in list(self._seen)[: len(self._seen) - SEEN_MAX]:
                del self._seen[k]
