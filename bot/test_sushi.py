"""Offline tests for the Sushi v7 client's status/error classification (urlopen is stubbed —
no network). The critical contract: NoWay and Partial are DETERMINISTIC verdicts (fail fast,
never retried), and a Partial response's output is never surfaced as a full-fill quote."""
import io
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("DRY_RUN", "1")

from bot import sushi  # noqa: E402

TOK_A = "0x" + "aa" * 20
TOK_B = "0x" + "bb" * 20
DEAD = "0x" + "de" * 20


def _resp(payload: dict):
    class _R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    return _R(json.dumps(payload).encode())


def _quote(**kw):
    return sushi.quote(TOK_A, TOK_B, 10 ** 18, sender=DEAD, recipient=DEAD,
                       timeout=1.0, **kw)


class TestStatusClassification(unittest.TestCase):
    def test_success_normalised(self):
        payload = {"status": "Success", "assumedAmountOut": "123", "priceImpact": 0.01,
                   "gasSpent": 1, "tx": {"to": sushi.ROUTE_PROCESSOR, "data": "0xdead"}}
        with mock.patch.object(sushi.urllib.request, "urlopen",
                               return_value=_resp(payload)) as m:
            q = _quote(retries=3)
        self.assertEqual(q["amount_out"], 123)
        self.assertEqual(m.call_count, 1)

    def test_noway_fails_fast_no_retry(self):
        with mock.patch.object(sushi.urllib.request, "urlopen",
                               side_effect=lambda *a, **k: _resp({"status": "NoWay"})) as m:
            with self.assertRaises(sushi.NoRouteError):
                _quote(retries=3)
        self.assertEqual(m.call_count, 1)                  # deterministic — one round-trip

    def test_partial_fails_fast_no_retry_distinct_type(self):
        # Partial = the route fills only PART of amountIn: deterministic for the (pair, size),
        # must never be retried as transient, and must never surface the partial output.
        payload = {"status": "Partial", "assumedAmountOut": "999999",
                   "tx": {"to": sushi.ROUTE_PROCESSOR, "data": "0xdead"}}
        with mock.patch.object(sushi.urllib.request, "urlopen",
                               side_effect=lambda *a, **k: _resp(payload)) as m:
            with self.assertRaises(sushi.PartialRouteError):
                _quote(retries=3)
        self.assertEqual(m.call_count, 1)                  # NOT retried as transient
        self.assertTrue(issubclass(sushi.PartialRouteError, sushi.SushiError))
        self.assertFalse(issubclass(sushi.PartialRouteError, sushi.NoRouteError))
        #                ^ Partial is NOT "no route at any size" — smaller sizes may fill

    def test_transient_oserror_is_retried(self):
        calls = {"n": 0}
        payload = {"status": "Success", "assumedAmountOut": "5", "priceImpact": 0,
                   "gasSpent": 0, "tx": {"to": sushi.ROUTE_PROCESSOR, "data": "0x"}}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("conn reset")
            return _resp(payload)
        with mock.patch.object(sushi.urllib.request, "urlopen", side_effect=flaky), \
                mock.patch.object(sushi.time, "sleep", lambda s: None):
            q = _quote(retries=3)
        self.assertEqual(q["amount_out"], 5)
        self.assertEqual(calls["n"], 2)                    # transient WAS retried


if __name__ == "__main__":
    unittest.main()
