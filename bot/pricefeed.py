"""Binance WS price feed for the oracle-push PREDICTION pre-arm layer (bot/predict.py; behind
KT_PREDICT, default-SHADOW — importing this module opens nothing).

WHY: Chainlink BTC/USD & ETH/USD on Katana push on-chain when the off-chain price deviates
~0.5% (or the 24h heartbeat). The off-chain price (proxied by Binance spot) crosses that
threshold a MEDIAN ~30-40s BEFORE the on-chain push, because the push lags by the OCR
round+consensus+tx latency. Watching Binance and predicting the push therefore buys ~30-40s to
be FULLY pre-armed — 60-80x the ~0.6s head start the mempool layer alone gives us. This feed is
the eyes; bot/predict.py is the anchor/return/hysteresis brain; the executor's pre-arm consumes
the signal. It NEVER fires anything (a prediction is a PREPARATION edge, not an overtake — the
position isn't liquidatable until the oracle reprices on-chain, and the exact per-round tip is
still read from the pending oracle tx in the mempool).

REACHABILITY (verified from this VPS 2026-07-17): TCP/TLS to stream.binance.com:9443 connects;
the WS handshake returns 101 in ~1s; both btcusdt@bookTicker + ethusdt@bookTicker stream on ONE
connection via an in-band SUBSCRIBE (the combined `/stream?streams=` query-string form is NOT
used — WsConn ignores the URL query, so we subscribe in-band exactly like mempool.py's
eth_subscribe). If Binance is ever blocked, point KT_PREDICT_WS_URL at a fallback venue (Coinbase
wss://ws-feed.exchange.coinbase.com, OKX wss://ws.okx.com:8443/ws/v5/public — both reachable from
here) and adapt _subscribe/_on_message; on WS down the feed simply goes unhealthy and the
prediction layer degrades to nothing (the mempool/fast-path fire behaviour is unaffected).

STRICT THREADING CONTRACT (mirrors bot/mempool.py): ONE background daemon thread owns its own
persistent websocket; the single-threaded main loop NEVER blocks on it — it reads a lock-guarded
mid-price snapshot. A dropped socket logs, marks unhealthy, and reconnects with capped backoff;
it never wedges or busy-loops. The transport is INJECTABLE so reconnect/parse is unit-tested with
zero network (test_pricefeed.py).
"""
from __future__ import annotations

import json
import os
import threading
import time

from bot.mempool import WsConn             # reuse the RFC6455 framing / ping-pong transport

WS_URL = os.environ.get("KT_PREDICT_WS_URL", "wss://stream.binance.com:9443/ws")
# bookTicker updates arrive many times/second; this much silence => stream wedged => reconnect.
STALE_SEC = float(os.environ.get("KT_PREDICT_STALE_SEC", "10.0"))
BACKOFF_MAX_SEC = float(os.environ.get("KT_PREDICT_BACKOFF_MAX", "30.0"))


class PriceFeed:
    """Background Binance WS reader. Construct with the symbols to track (upper-case, e.g.
    "BTCUSDT"), call start(); read mid(symbol) / healthy() from the main loop. on_tick(symbol,
    mid, wall) — if given — fires in THIS thread on every update (keep it fast + thread-safe).

    Subscribes to `<symbol lower>@bookTicker` for each symbol and tracks the mid = (bid+ask)/2.
    bookTicker is best-bid/ask only (no trade needed to move), so the mid tracks the venue price
    with sub-second latency and no aggregation lag."""

    def __init__(self, symbols=("BTCUSDT", "ETHUSDT"), on_tick=None, connect=None,
                 ws_url: str = WS_URL, now=time.monotonic, sleep=time.sleep, log=print,
                 wall=time.time, stale_sec: float = STALE_SEC,
                 backoff_max: float = BACKOFF_MAX_SEC):
        self.symbols = tuple(s.upper() for s in symbols)
        self.on_tick = on_tick
        self._connect = connect or (lambda: WsConn(ws_url))
        self._now, self._sleep, self._log, self._wall = now, sleep, log, wall
        self.stale_sec, self.backoff_max = stale_sec, backoff_max
        self._lock = threading.Lock()
        self._mid: dict[str, float] = {}
        self._connected = False
        self._last_msg = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats = {"reconnects": 0, "ticks": 0, "parse_fail": 0}

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="predict-pricefeed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._connected = False

    # -- main loop reads these (thread-safe) ------------------------------------
    def mid(self, symbol: str) -> float | None:
        with self._lock:
            return self._mid.get(symbol.upper())

    def healthy(self) -> bool:
        """Connected AND a message within stale_sec. On unhealthy the prediction layer simply
        stops predicting — the mempool/fast-path fire behaviour is entirely independent."""
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
                self._log(f"[predict] Binance WS connected + subscribed "
                          f"({', '.join(self.symbols)} bookTicker)")
                backoff = 0.5
                self._serve(conn)
            except Exception as e:                       # WsClosed, socket errors, parse bugs
                self._log(f"[predict] Binance WS dropped ({type(e).__name__}: {str(e)[:120]}) — "
                          f"reconnecting in {backoff:.1f}s; fire path unaffected")
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
        # in-band SUBSCRIBE (both symbols on one connection); avoids the `/stream?streams=` query
        # string that WsConn's URL parser drops. The node replies {"result":null,"id":1}.
        params = [f"{s.lower()}@bookTicker" for s in self.symbols]
        conn.send_text(json.dumps({"method": "SUBSCRIBE", "params": params, "id": 1}))

    def _serve(self, conn) -> None:
        while not self._stop.is_set():
            msg = conn.recv_message()
            with self._lock:
                self._last_msg = self._now()
            try:
                j = json.loads(msg)
            except ValueError:
                self.stats["parse_fail"] += 1
                continue
            self._on_message(j)

    def _on_message(self, j: dict) -> None:
        if not isinstance(j, dict):
            return
        if "result" in j and "id" in j:                  # SUBSCRIBE ack — no data
            return
        data = j.get("data", j)                          # tolerate the /stream wrapper too
        sym = (data.get("s") or "").upper()
        bid, ask = data.get("b"), data.get("a")
        if not sym or bid is None or ask is None:
            return
        try:
            mid = (float(bid) + float(ask)) / 2.0
        except (TypeError, ValueError):
            self.stats["parse_fail"] += 1
            return
        if mid <= 0:
            return
        with self._lock:
            self._mid[sym] = mid
        self.stats["ticks"] += 1
        if self.on_tick is not None:
            try:
                self.on_tick(sym, mid, self._wall())
            except Exception as e:
                self._log(f"[predict] on_tick error: {str(e)[:120]}")
