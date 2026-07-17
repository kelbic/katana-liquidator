"""Offline tests for the mempool WSS manager (no network, no real threads/sleeps).

The manager runs a background socket that the single-threaded fire loop must never block on and
that must degrade cleanly when the WSS drops. These tests drive its internals with injected
fakes: framing round-trips, reconnect/backoff, oracle-tx detection + tip extraction, the
head snapshot, shadow landing-resolution, and callback-error isolation."""
import json
import os
import unittest

os.environ.setdefault("DRY_RUN", "1")

from analysis.protocols import MARKETS               # noqa: E402
from bot import mempool as mp                          # noqa: E402
from bot import oracles as oc                          # noqa: E402

BTC_AGG = "0x56ac2b1b78225d47993e8866795a34ad540a515c"
BTC_ONLY_EOA = "0x7f333870a01566fac9b7207c0abd096c761914d6"
WBTC_USDC = oc._lc(MARKETS["vbWBTC/vbUSDC"]["id"])
WBTC_USDT = oc._lc(MARKETS["vbWBTC/vbUSDT"]["id"])


# --- framing ---------------------------------------------------------------------------------
def _reader(data: bytes):
    """recv_exactly over an in-memory buffer (raises if it runs dry, like a closed socket)."""
    box = {"b": data}

    def recv_exactly(n):
        if len(box["b"]) < n:
            raise mp.WsClosed("drained")
        out, box["b"] = box["b"][:n], box["b"][n:]
        return out
    return recv_exactly


class TestFraming(unittest.TestCase):
    def test_masked_text_roundtrip(self):
        payload = b'{"jsonrpc":"2.0"}'
        fin, op, data = mp.read_frame(_reader(mp.encode_frame(payload, mp.OP_TEXT, mask=True)))
        self.assertTrue(fin)
        self.assertEqual(op, mp.OP_TEXT)
        self.assertEqual(data, payload)

    def test_unmasked_server_frame(self):
        payload = b"hello"
        fin, op, data = mp.read_frame(_reader(mp.encode_frame(payload, mp.OP_TEXT, mask=False)))
        self.assertEqual(data, payload)

    def test_extended_length_126_and_127(self):
        for n in (200, 70000):                            # forces 2-byte then 8-byte length
            payload = b"x" * n
            fin, op, data = mp.read_frame(_reader(mp.encode_frame(payload, mask=False)))
            self.assertEqual(len(data), n)

    def test_fragmented_message_reassembles(self):
        # two frames: TEXT(fin=0) + CONT(fin=1) — the concatenation is the message
        part1 = bytearray(mp.encode_frame(b'{"a":', mp.OP_TEXT, mask=False))
        part1[0] &= 0x7F                                  # clear FIN on the first frame
        part2 = mp.encode_frame(b'1}', mp.OP_CONT, mask=False)
        rd = _reader(bytes(part1) + part2)
        fin1, op1, d1 = mp.read_frame(rd)
        fin2, op2, d2 = mp.read_frame(rd)
        self.assertFalse(fin1)
        self.assertEqual(op1, mp.OP_TEXT)
        self.assertTrue(fin2)
        self.assertEqual(op2, mp.OP_CONT)
        self.assertEqual((d1 + d2).decode(), '{"a":1}')


class _FakeSocket:
    """Minimal socket for WsConn.recv_message: serves queued inbound bytes, records sends."""
    def __init__(self, inbound: bytes):
        self._in = inbound
        self.sent = bytearray()

    def settimeout(self, t):
        pass

    def recv(self, n):
        if not self._in:
            return b""                                    # -> WsClosed in _recv_exactly
        out, self._in = self._in[:n], self._in[n:]
        return out

    def sendall(self, b):
        self.sent += b


def _wsconn(inbound: bytes) -> mp.WsConn:
    c = object.__new__(mp.WsConn)                         # bypass the network handshake
    c._sock = _FakeSocket(inbound)
    c.timeout = 1.0
    return c


class TestWsConnRecv(unittest.TestCase):
    def test_reassembles_and_answers_ping(self):
        ping = mp.encode_frame(b"pingdata", mp.OP_PING, mask=False)
        f1 = bytearray(mp.encode_frame(b"foo", mp.OP_TEXT, mask=False))
        f1[0] &= 0x7F                                     # fin=0
        f2 = mp.encode_frame(b"bar", mp.OP_CONT, mask=False)
        conn = _wsconn(ping + bytes(f1) + f2)
        msg = conn.recv_message()
        self.assertEqual(msg, "foobar")
        # a pong (opcode 0xA) was sent in reply to the ping
        fin, op, data = mp.read_frame(_reader(bytes(conn._sock.sent)))
        self.assertEqual(op, mp.OP_PONG)
        self.assertEqual(data, b"pingdata")

    def test_close_frame_raises(self):
        conn = _wsconn(mp.encode_frame(b"", mp.OP_CLOSE, mask=False))
        with self.assertRaises(mp.WsClosed):
            conn.recv_message()


# --- manager helpers -------------------------------------------------------------------------
class _Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, d):
        self.slept.append(d)
        self.t += d


def _ack(id_, sub):
    return json.dumps({"jsonrpc": "2.0", "id": id_, "result": sub})


def _notif(sub, result):
    return json.dumps({"jsonrpc": "2.0", "method": "eth_subscription",
                       "params": {"subscription": sub, "result": result}})


def _head(block, ts):
    return {"number": hex(block), "timestamp": hex(ts)}


def _oracle_tx(tx_hash, tip_wei=3376432):
    return {"hash": tx_hash, "from": BTC_ONLY_EOA, "to": "0x120e6016cde",
            "maxPriorityFeePerGas": hex(tip_wei), "maxFeePerGas": hex(tip_wei + 1_400_000)}


class _FakeConn:
    """Injected transport: yields queued messages, then raises WsClosed. Records sends."""
    def __init__(self, messages):
        self.sent = []
        self._msgs = list(messages)
        self.closed = False

    def send_text(self, s):
        self.sent.append(s)

    def recv_message(self, timeout=None):
        if self._msgs:
            return self._msgs.pop(0)
        raise mp.WsClosed("drained")

    def close(self):
        self.closed = True


def _client(on_signal=None, on_resolve=None, fetch_tx=None, connect=None, clock=None):
    clock = clock or _Clock()
    return mp.MempoolClient(
        on_signal=on_signal or (lambda s: None), on_resolve=on_resolve,
        fetch_tx=fetch_tx if fetch_tx is not None else (lambda h: None),
        connect=connect, now=clock.now, sleep=clock.sleep, log=lambda *a: None), clock


class TestDetection(unittest.TestCase):
    def test_full_body_oracle_tx_signals(self):
        sigs = []
        c, _ = _client(on_signal=sigs.append)
        c._sub_pending = "0xpend"
        c._on_head(_head(100, 1784268572))
        c._on_pending(_oracle_tx("0x" + "aa" * 32, tip_wei=3376432))
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertEqual(s.market_ids, {WBTC_USDC, WBTC_USDT})   # BTC transmitter -> WBTC mkts
        self.assertEqual(s.tip_wei, 3376432)                     # matched to the wei
        self.assertEqual(s.head_block, 100)
        self.assertIsNotNone(s.head_age_ms)

    def test_hash_only_stream_fetches_body(self):
        fetched = []

        def fetch(h):
            fetched.append(h)
            return _oracle_tx(h)
        sigs = []
        c, _ = _client(on_signal=sigs.append, fetch_tx=fetch)
        c._on_pending("0x" + "bb" * 32)                          # bare hash
        self.assertEqual(fetched, ["0x" + "bb" * 32])
        self.assertEqual(len(sigs), 1)

    def test_non_oracle_tx_ignored(self):
        sigs = []
        c, _ = _client(on_signal=sigs.append)
        c._on_pending({"hash": "0x" + "cc" * 32, "from": "0x" + "de" * 20,
                       "to": "0x" + "ad" * 20, "maxPriorityFeePerGas": "0x1"})
        self.assertEqual(sigs, [])

    def test_pending_hash_deduped(self):
        sigs = []
        c, _ = _client(on_signal=sigs.append)
        tx = _oracle_tx("0x" + "dd" * 32)
        c._on_pending(tx)
        c._on_pending(tx)                                        # same hash again
        self.assertEqual(len(sigs), 1)

    def test_fetch_returns_none_no_signal(self):
        sigs = []
        c, _ = _client(on_signal=sigs.append, fetch_tx=lambda h: None)
        c._on_pending("0x" + "ee" * 32)
        self.assertEqual(sigs, [])
        self.assertEqual(c.stats["fetch_fail"], 1)

    def test_on_signal_error_does_not_propagate(self):
        def boom(s):
            raise RuntimeError("callback bug")
        c, _ = _client(on_signal=boom)
        c._on_pending(_oracle_tx("0x" + "ff" * 32))              # must not raise
        self.assertEqual(c.stats["oracle_hits"], 1)


class TestHeadSnapshot(unittest.TestCase):
    def test_latest_head_and_age(self):
        c, clock = _client()
        c._on_head(_head(37525761, 1784268572))
        clock.t += 0.05                                          # 50ms later
        h = c.latest_head()
        self.assertEqual(h["block"], 37525761)
        self.assertEqual(h["ts"], 1784268572)
        h["block"] = -1                                          # snapshot is a copy...
        self.assertEqual(c.latest_head()["block"], 37525761)     # ...mutating it is harmless
        # a pending detected 50ms into the block reports head_age_ms ~50
        sigs = []
        c.on_signal = sigs.append
        c._on_pending(_oracle_tx("0x" + "12" * 32))
        self.assertAlmostEqual(sigs[0].head_age_ms, 50.0, places=3)

    def test_malformed_head_ignored(self):
        c, _ = _client()
        c._on_head({"number": "not-hex"})
        self.assertIsNone(c.latest_head())


class TestResolution(unittest.TestCase):
    def test_landed_block_resolved_on_head(self):
        resolved = []
        landed = {"n": None}

        def fetch(h):
            return _oracle_tx(h) if landed["n"] is None else {
                **_oracle_tx(h), "blockNumber": hex(landed["n"])}
        c, _ = _client(on_signal=lambda s: None, on_resolve=resolved.append, fetch_tx=fetch)
        c._on_head(_head(200, 1784268600))
        c._on_pending(_oracle_tx("0x" + "34" * 32))
        self.assertEqual(len(c._resolving), 1)
        landed["n"] = 201                                        # now it lands
        c._on_head(_head(201, 1784268601))
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].landed_block, 201)
        self.assertEqual(c._resolving, [])                       # cleared once resolved

    def test_unresolved_dropped_after_budget(self):
        resolved = []
        c, _ = _client(on_signal=lambda s: None, on_resolve=resolved.append,
                       fetch_tx=lambda h: _oracle_tx(h))         # never lands (no blockNumber)
        c._on_head(_head(300, 1784268700))
        c._on_pending(_oracle_tx("0x" + "56" * 32))
        for b in range(301, 301 + mp.RESOLVE_HEADS):
            c._on_head(_head(b, 1784268700 + (b - 300)))
        self.assertEqual(len(resolved), 1)                       # reported once, as unresolved
        self.assertIsNone(resolved[0].landed_block)
        self.assertEqual(c._resolving, [])


class TestServeDispatch(unittest.TestCase):
    def test_serve_routes_head_and_pending(self):
        sigs = []
        c, _ = _client(on_signal=sigs.append)
        conn = _FakeConn([
            _ack(1, "0xhead"), _ack(2, "0xpend"),
            _notif("0xhead", _head(400, 1784268800)),
            _notif("0xpend", _oracle_tx("0x" + "9a" * 32)),
        ])
        try:
            c._serve(conn)
        except mp.WsClosed:
            pass
        self.assertEqual(c.latest_head()["block"], 400)
        self.assertEqual(len(sigs), 1)


class TestReconnect(unittest.TestCase):
    def test_reconnects_with_capped_backoff_and_stays_alive(self):
        clock = _Clock()
        attempts = {"n": 0}

        def connect():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("connection refused")             # first attempt fails
            if attempts["n"] == 2:
                return _FakeConn([_ack(1, "0xhead"), _ack(2, "0xpend"),
                                  _notif("0xhead", _head(500, 1784269000))])
            clock_stop()                                        # third: stop the loop
            return _FakeConn([])

        c, _ = _client(connect=connect, clock=clock)

        def clock_stop():
            c._stop.set()
        c._run()
        self.assertGreaterEqual(attempts["n"], 2)
        self.assertTrue(len(clock.slept) >= 1)
        self.assertEqual(clock.slept[0], 0.5)                   # first backoff
        if len(clock.slept) >= 2:
            self.assertGreaterEqual(clock.slept[1], clock.slept[0])   # backoff grows
        self.assertGreater(c.stats["reconnects"], 0)

    def test_subscribe_sends_both_subscriptions(self):
        conns = []

        def connect():
            k = _FakeConn([])
            conns.append(k)
            c._stop.set()                                       # one connect, then stop
            return k
        c, _ = _client(connect=connect)
        c._run()
        subs = [json.loads(s)["params"][0] for s in conns[0].sent]
        self.assertIn("newHeads", subs)
        self.assertIn("newPendingTransactions", subs)


class TestHealth(unittest.TestCase):
    def test_unhealthy_when_stale_or_disconnected(self):
        c, clock = _client()
        self.assertFalse(c.healthy())                           # never connected
        with c._lock:
            c._connected = True
            c._last_msg = clock.now()
        self.assertTrue(c.healthy())
        clock.t += c.stale_sec + 1                              # go stale
        self.assertFalse(c.healthy())


if __name__ == "__main__":
    unittest.main()
