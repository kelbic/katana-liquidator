"""Offline tests for the keep-alive RPC transport (no network): connection pooling, the
reconnect-once contract, non-200 classification, and the fire-path helpers
poll_block_number/warm. The connection factory is stubbed — no sockets are opened."""
import http.client
import json
import unittest

from analysis import rpc as rpc_mod
from analysis.rpc import HttpStatusError, Rpc, RpcError


class _FakeResponse:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return self._payload


class _FakeConn:
    """Scripted connection: each request consumes the next behavior —
    bytes payload -> 200 response; int -> that HTTP status; Exception -> raised."""
    def __init__(self, script: list):
        self.script = list(script)
        self.requests = 0
        self.closed = False
        self.sock = None            # rpc only touches .sock when not None

    def request(self, *a, **k):
        self.requests += 1
        nxt = self.script[0]
        if isinstance(nxt, Exception):
            self.script.pop(0)
            raise nxt

    def getresponse(self):
        nxt = self.script.pop(0)
        if isinstance(nxt, int):
            return _FakeResponse(b"rate limited", status=nxt)
        return _FakeResponse(nxt)

    def close(self):
        self.closed = True


def _result(value) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": value}).encode()


class PoolTestCase(unittest.TestCase):
    """Base: isolate the module pool and stub the connection factory."""

    def setUp(self):
        self._save_pool = dict(rpc_mod._POOL)
        self._save_connect = rpc_mod._connect
        rpc_mod._POOL.clear()
        self.made: list[_FakeConn] = []
        self.scripts: list[list] = []

        def connect(scheme, netloc, timeout):
            conn = _FakeConn(self.scripts.pop(0) if self.scripts else [_result("0x0")])
            self.made.append(conn)
            return conn
        rpc_mod._connect = connect

    def tearDown(self):
        rpc_mod._POOL.clear()
        rpc_mod._POOL.update(self._save_pool)
        rpc_mod._connect = self._save_connect

    def _rpc(self, **kw) -> Rpc:
        kw.setdefault("urls", ["https://rpc.test"])
        kw.setdefault("min_interval", 0.0)
        kw.setdefault("retries", 1)
        return Rpc(**kw)


class TestKeepAlive(PoolTestCase):
    def test_connection_is_reused_across_calls_and_instances(self):
        # the pool is module-level: a fresh Rpc per pass (executor pattern) must NOT reopen
        self.scripts = [[_result("0x10"), _result("0x11")]]
        self.assertEqual(self._rpc().block_number(), 0x10)
        self.assertEqual(self._rpc().block_number(), 0x11)
        self.assertEqual(len(self.made), 1)                  # one socket, two calls
        self.assertEqual(self.made[0].requests, 2)

    def test_stale_socket_reconnects_once_transparently(self):
        # LB idle-timeout kill: first request on the pooled socket fails -> ONE fresh
        # reconnect serves the call; no Rpc-level retry is consumed
        self.scripts = [[BrokenPipeError("stale")], [_result("0x2a")]]
        self.assertEqual(self._rpc(retries=1).block_number(), 0x2a)
        self.assertEqual(len(self.made), 2)
        self.assertTrue(self.made[0].closed)                 # stale socket dropped from pool

    def test_both_attempts_failing_exhausts_call(self):
        self.scripts = [[ConnectionResetError("a")], [ConnectionResetError("b")]]
        with self.assertRaises(RuntimeError):
            self._rpc(retries=1).block_number()

    def test_non_200_is_transport_not_result(self):
        # 429/5xx bodies must never be parsed as a result; socket stays pooled (body drained)
        self.scripts = [[429, _result("0x5")]]
        r = self._rpc(retries=2, backoff_429=0.0)
        self.assertEqual(r.block_number(), 0x5)
        self.assertEqual(len(self.made), 1)                  # same socket after the 429

    def test_rpc_error_still_raises_and_never_retries(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "error": {"code": -32000, "message": "boom"}}).encode()
        self.scripts = [[body, _result("0x1")]]
        with self.assertRaises(RpcError):
            self._rpc(retries=3).block_number()
        self.assertEqual(self.made[0].requests, 1)           # no retry on a JSON-RPC error

    def test_read_whitelist_enforced(self):
        with self.assertRaises(ValueError):
            self._rpc().call("eth_sendRawTransaction", ["0x00"])

    def test_endpoint_rotation_on_retry(self):
        # first endpoint down hard -> second attempt goes to the fallback url
        self.scripts = [[OSError("down")], [OSError("down")], [_result("0x7")]]
        r = Rpc(urls=["https://a.test", "https://b.test"], retries=2, min_interval=0.0)
        self.assertEqual(r.block_number(), 0x7)
        self.assertEqual(len(self.made), 3)                  # a (stale+fresh), then b


class TestFirePathHelpers(PoolTestCase):
    def test_poll_block_number_single_shot(self):
        self.scripts = [[_result(hex(37_000_001))]]
        self.assertEqual(self._rpc().poll_block_number(), 37_000_001)

    def test_poll_block_number_none_on_error_no_raise(self):
        # one reconnect attempt max, then None — the next 18ms tick is the retry
        self.scripts = [[OSError("down")], [OSError("down")]]
        self.assertIsNone(self._rpc().poll_block_number())
        self.assertEqual(len(self.made), 2)

    def test_poll_block_number_none_on_garbage(self):
        self.scripts = [[b"<html>502</html>"]]
        self.assertIsNone(self._rpc().poll_block_number())

    def test_warm_opens_socket_and_swallows_errors(self):
        self.scripts = [[_result("0xb67d2"), _result("0x1")]]
        r = self._rpc()
        self.assertTrue(r.warm())
        self.assertEqual(len(self.made), 1)
        self.assertEqual(r.block_number(), 1)                # warmed socket is the one reused
        self.assertEqual(self.made[0].requests, 2)
        self.scripts = [[OSError("down")], [OSError("down")]]
        rpc_mod._POOL.clear()
        self.assertFalse(Rpc(urls=["https://c.test"]).warm())  # never raises

    def test_http_status_error_carries_status(self):
        self.assertEqual(HttpStatusError(503).status, 503)
        self.assertIsInstance(HttpStatusError(503), OSError)

    def test_incomplete_read_still_retryable(self):
        # get_logs_chunked depends on IncompleteRead being caught by call()'s retry loop
        self.scripts = [[http.client.IncompleteRead(b"x")], [_result("0x3")]]
        self.assertEqual(self._rpc(retries=2).block_number(), 0x3)


if __name__ == "__main__":
    unittest.main()
