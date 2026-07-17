"""Offline tests for the Binance WS price feed (no network, no real threads/sleeps).

The feed runs a background socket the single-threaded fire loop must never block on, and must
degrade cleanly when the WS drops. These drive its internals with injected fakes: subscribe,
bookTicker parse -> mid snapshot, reconnect/backoff, health, and callback-error isolation."""
import json
import os
import unittest

os.environ.setdefault("DRY_RUN", "1")

from bot import pricefeed as pf                    # noqa: E402
from bot import mempool as mp                       # noqa: E402


class _Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, d):
        self.slept.append(d)
        self.t += d


def _book(symbol, bid, ask):
    return json.dumps({"u": 1, "s": symbol, "b": str(bid), "B": "1", "a": str(ask), "A": "1"})


class _FakeConn:
    """Injected transport: yields queued messages, then raises WsClosed. Records sends."""
    def __init__(self, messages):
        self.sent = []
        self._msgs = list(messages)

    def send_text(self, s):
        self.sent.append(s)

    def recv_message(self, timeout=None):
        if self._msgs:
            return self._msgs.pop(0)
        raise mp.WsClosed("drained")

    def close(self):
        pass


def _feed(connect=None, on_tick=None, clock=None, symbols=("BTCUSDT", "ETHUSDT")):
    clock = clock or _Clock()
    return pf.PriceFeed(symbols=symbols, on_tick=on_tick, connect=connect,
                        now=clock.now, sleep=clock.sleep, log=lambda *a: None,
                        wall=lambda: 42.0), clock


class TestParse(unittest.TestCase):
    def test_booticker_sets_mid(self):
        f, _ = _feed()
        f._on_message(json.loads(_book("BTCUSDT", 63000, 63002)))
        self.assertEqual(f.mid("BTCUSDT"), 63001.0)        # (bid+ask)/2
        self.assertEqual(f.mid("btcusdt"), 63001.0)        # case-insensitive
        self.assertIsNone(f.mid("ETHUSDT"))                # untouched
        self.assertEqual(f.stats["ticks"], 1)

    def test_sub_ack_ignored(self):
        f, _ = _feed()
        f._on_message({"result": None, "id": 1})           # subscribe ack, no data
        self.assertIsNone(f.mid("BTCUSDT"))
        self.assertEqual(f.stats["ticks"], 0)

    def test_stream_wrapper_parsed(self):
        f, _ = _feed()
        f._on_message({"stream": "ethusdt@bookTicker",
                       "data": {"s": "ETHUSDT", "b": "1800.0", "a": "1802.0"}})
        self.assertEqual(f.mid("ETHUSDT"), 1801.0)

    def test_malformed_no_mid(self):
        f, _ = _feed()
        f._on_message({"s": "BTCUSDT"})                    # no bid/ask
        f._on_message({"s": "BTCUSDT", "b": "x", "a": "y"})  # unparseable
        f._on_message({"s": "BTCUSDT", "b": "-1", "a": "-1"})  # non-positive mid
        self.assertIsNone(f.mid("BTCUSDT"))
        self.assertEqual(f.stats["ticks"], 0)

    def test_on_tick_fires_and_errors_isolated(self):
        seen = []

        def boom(sym, mid, wall):
            seen.append((sym, mid, wall))
            raise RuntimeError("callback bug")
        f, _ = _feed(on_tick=boom)
        f._on_message(json.loads(_book("BTCUSDT", 100, 102)))   # must not raise
        self.assertEqual(seen, [("BTCUSDT", 101.0, 42.0)])
        self.assertEqual(f.mid("BTCUSDT"), 101.0)               # still recorded


class TestServeAndSubscribe(unittest.TestCase):
    def test_subscribe_sends_both_booktickers(self):
        conns = []

        def connect():
            k = _FakeConn([])
            conns.append(k)
            f._stop.set()                                       # one connect, then stop
            return k
        f, _ = _feed(connect=connect)
        f._run()
        sub = json.loads(conns[0].sent[0])
        self.assertEqual(sub["method"], "SUBSCRIBE")
        self.assertEqual(set(sub["params"]), {"btcusdt@bookTicker", "ethusdt@bookTicker"})

    def test_serve_routes_booktickers(self):
        f, _ = _feed()
        conn = _FakeConn([_book("BTCUSDT", 63000, 63004), _book("ETHUSDT", 1800, 1800)])
        try:
            f._serve(conn)
        except mp.WsClosed:
            pass
        self.assertEqual(f.mid("BTCUSDT"), 63002.0)
        self.assertEqual(f.mid("ETHUSDT"), 1800.0)


class TestReconnect(unittest.TestCase):
    def test_reconnects_with_capped_backoff_and_stays_alive(self):
        clock = _Clock()
        attempts = {"n": 0}

        def connect():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("connection refused")             # first attempt fails
            if attempts["n"] == 2:
                return _FakeConn([_book("BTCUSDT", 63000, 63002)])
            f._stop.set()                                       # third: stop the loop
            return _FakeConn([])
        f, _ = _feed(connect=connect, clock=clock)
        f._run()
        self.assertGreaterEqual(attempts["n"], 2)
        self.assertEqual(clock.slept[0], 0.5)                   # first backoff
        if len(clock.slept) >= 2:
            self.assertGreaterEqual(clock.slept[1], clock.slept[0])   # grows
        self.assertGreater(f.stats["reconnects"], 0)
        self.assertEqual(f.mid("BTCUSDT"), 63001.0)            # data from the good connection


class TestHealth(unittest.TestCase):
    def test_unhealthy_when_stale_or_disconnected(self):
        f, clock = _feed()
        self.assertFalse(f.healthy())                          # never connected
        with f._lock:
            f._connected = True
            f._last_msg = clock.now()
        self.assertTrue(f.healthy())
        clock.t += f.stale_sec + 1                             # go stale
        self.assertFalse(f.healthy())


if __name__ == "__main__":
    unittest.main()
