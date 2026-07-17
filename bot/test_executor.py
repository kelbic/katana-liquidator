"""Offline tests for the executor's pure logic (no RPC, no network). The Sushi quote is stubbed
so chunk selection, the profit gate, and calldata encoding are tested deterministically."""
import json
import os
import time
import unittest
import urllib.parse

os.environ.setdefault("DRY_RUN", "1")
os.environ.setdefault("KT_MIN_PROFIT_USD", "20")
os.environ.setdefault("KT_MAX_IMPACT", "0.02")
os.environ.setdefault("KT_CONTRACT", "0x000000000000000000000000000000000000bEEF")

from bot import executor as ex  # noqa: E402

VBUSDC = "0x203A662b0BD271A6ed5a60EdFbd04bFce608FD36"
VBWBTC = "0x0913DA6Da4b42f538B445599b46Bb4622342Cf52"


def _target(seized_btc=1.0, repaid_usdc=61000.0):
    return {"market_id": "0x" + "cd" * 32, "borrower": "0x" + "14" * 20,
            "loan": VBUSDC, "coll": VBWBTC,
            "oracle": "0xB60F728BdcE5e3921C0E42c1a6F07A1313D0040e",
            "irm": "0x4F708C0ae7deD3d74736594C2109C2E3c065B428", "lltv": 860000000000000000,
            "hf": 0.98, "debt_assets": int(repaid_usdc * 1e6),
            "repaid_assets": int(repaid_usdc * 1e6), "seized_assets": int(seized_btc * 1e8),
            "borrow_shares_repaid": int(repaid_usdc * 1e6 * 1000),
            # oracle scale: loan wei per coll wei * 1e36 (vbUSDC 6dec / vbWBTC 8dec)
            "price": int(repaid_usdc * 1e6 / 1e8 * 1e36)}


def _stub_quote(out_per_btc, impact):
    """Return a fake quote fn: out = amount_in(vbWBTC 8dec)/1e8 * out_per_btc (vbUSDC 6dec)."""
    def q(token_in, token_out, amount_in_wei, sender, recipient, max_slippage=0.005, **kw):
        out = int(amount_in_wei / 1e8 * out_per_btc * 1e6)
        return {"amount_out": out, "price_impact": impact, "gas": 400000,
                "swap_target": ex.SWAP_INPUT_HAIRCUT and "0xAC4c6e212A361c968F1725b4d055b47E63F80b75",
                "swap_calldata": "0xdeadbeef"}
    return q


class TestEvaluate(unittest.TestCase):
    def setUp(self):
        self._orig = ex.quote

    def tearDown(self):
        ex.quote = self._orig

    def test_full_chunk_when_profitable(self):
        # 1 vbWBTC seized, LIF 4.38% => repaid ~ seized/1.0438. out_per_btc high, low impact.
        ex.quote = _stub_quote(out_per_btc=61500, impact=0.008)
        t = _target(1.0, 61000.0)
        ev = ex.evaluate(None, t, gas_usd=0.01)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["f"], 1.0)               # full close is profitable
        self.assertGreater(ev["net_usd"], 20)

    def test_chunks_down_when_impact_too_high(self):
        # full close impact exceeds MAX_IMPACT (0.02) -> must pick a smaller fraction
        def q(token_in, token_out, amount_in_wei, sender, recipient, max_slippage=0.005, **kw):
            frac = amount_in_wei / (1.0 * 1e8)       # relative to full 1 btc
            impact = 0.03 * frac                     # impact grows with size; >0.02 at full
            out = int(amount_in_wei / 1e8 * 63000 * 1e6)  # ~3.3% over repaid (LIF baked in)
            return {"amount_out": out, "price_impact": impact, "gas": 400000,
                    "swap_target": "0xAC4c6e212A361c968F1725b4d055b47E63F80b75",
                    "swap_calldata": "0xbeef"}
        ex.quote = q
        t = _target(1.0, 61000.0)
        ev = ex.evaluate(None, t, gas_usd=0.01)
        self.assertIsNotNone(ev)
        self.assertLess(ev["f"], 1.0)                # had to chunk down
        self.assertLessEqual(ev["impact"], ex.MAX_IMPACT + 1e-9)

    def test_none_when_unprofitable(self):
        # proceeds below repaid at every size -> no chunk clears the floor
        ex.quote = _stub_quote(out_per_btc=50000, impact=0.005)  # way below repaid
        t = _target(1.0, 61000.0)
        self.assertIsNone(ex.evaluate(None, t, gas_usd=0.01))

    def test_none_and_no_chunking_on_no_route(self):
        # dead/exotic collateral (Sushi NoWay) -> skip immediately, don't try every fraction
        calls = {"n": 0}

        def q(*a, **k):
            calls["n"] += 1
            raise ex.NoRouteError("no route (NoWay)")
        ex.quote = q
        t = _target(1.0, 61000.0)
        self.assertIsNone(ex.evaluate(None, t, gas_usd=0.01))
        self.assertEqual(calls["n"], 1)   # bailed on the first quote, no chunk-down loop

    def test_minprofit_floor_set_for_stable(self):
        ex.quote = _stub_quote(out_per_btc=61500, impact=0.008)
        t = _target(1.0, 61000.0)
        ev = ex.evaluate(None, t, gas_usd=0.01)
        # on-chain floor = USD floor AND at least half the quoted net (H1/M1)
        self.assertEqual(ev["min_profit_wei"],
                         max(int(ex.MIN_PROFIT_USD * 1e6), ev["net_wei"] // 2))
        self.assertGreaterEqual(ev["min_profit_wei"], int(ex.MIN_PROFIT_USD * 1e6))

    def test_exact_shares_above_float53(self):
        # C1: an 18-dec loan's borrowShares run ~1e27 — far past float64's 2^53 exact range.
        # int(shares * 1.0) rounds to the nearest representable float and Morpho's checked
        # `borrowShares -= repaidShares` would Panic(0x11). Sizing must be EXACT integer math.
        ex.quote = _stub_quote(out_per_btc=61500, impact=0.008)
        shares = 10 ** 27 + 7                       # deliberately not float-representable
        t = _target(1.0, 61000.0)
        t["borrow_shares_repaid"] = shares
        ev = ex.evaluate(None, t, gas_usd=0.01)
        self.assertEqual(ev["f"], 1.0)
        self.assertEqual(ev["repaid_shares"], shares)               # bit-exact at full close
        self.assertNotEqual(ev["repaid_shares"], int(shares * 1.0))  # the old bug

    def test_exact_shares_fractional_chunk(self):
        # chunked close: shares must scale as an exact rational, floor-rounded (never over)
        def q(token_in, token_out, amount_in_wei, sender, recipient, max_slippage=0.005, **kw):
            frac = amount_in_wei / (1.0 * 1e8)
            impact = 0.03 * frac                    # forces a chunk-down
            out = int(amount_in_wei / 1e8 * 63000 * 1e6)
            return {"amount_out": out, "price_impact": impact, "gas": 400000,
                    "swap_target": "0xAC4c6e212A361c968F1725b4d055b47E63F80b75",
                    "swap_calldata": "0xbeef"}
        ex.quote = q
        shares = 10 ** 27 + 1
        t = _target(1.0, 61000.0)
        t["borrow_shares_repaid"] = shares
        ev = ex.evaluate(None, t, gas_usd=0.01)
        self.assertLess(ev["f"], 1.0)
        num, den = [fr for fr in ex.CHUNK_FRACTIONS if fr[0] / fr[1] == ev["f"]][0]
        self.assertEqual(ev["repaid_shares"], shares * num // den)
        self.assertLessEqual(ev["repaid_shares"], shares)


class TestArmQuoteTimeoutCap(unittest.TestCase):
    """Small fix: an arm-path quote (deadline_mono set) must cap its Sushi timeout to the
    REMAINING idle budget and single-shot it, so a slow/Partial quote can't eat the armed
    window. The classic path (deadline_mono=None) keeps the full QUOTE_TIMEOUT + retries."""

    def setUp(self):
        self._orig = ex.quote

    def tearDown(self):
        ex.quote = self._orig

    def test_arm_quote_timeout_capped_to_remaining_budget(self):
        seen = {}

        def q(token_in, token_out, amount_in_wei, sender, recipient, max_slippage=0.005,
              timeout=5.0, retries=2):
            seen["timeout"] = timeout
            seen["retries"] = retries
            out = int(amount_in_wei / 1e8 * 61500 * 1e6)
            return {"amount_out": out, "price_impact": 0.008, "gas": 400000,
                    "swap_target": "0x" + "ac" * 20, "swap_calldata": "0xbeef"}
        ex.quote = q
        t = _target(1.0, 61000.0)
        # ~0.5s of idle budget left -> the quote must be capped at <= 0.5s and single-shot
        ex.evaluate(None, t, gas_usd=0.01, deadline_mono=time.monotonic() + 0.5)
        self.assertLessEqual(seen["timeout"], 0.5 + 1e-6)
        self.assertGreater(seen["timeout"], 0.0)
        self.assertEqual(seen["retries"], 1)

    def test_classic_path_uses_full_timeout(self):
        seen = {}

        def q(token_in, token_out, amount_in_wei, sender, recipient, max_slippage=0.005,
              timeout=5.0, retries=2):
            seen["timeout"] = timeout
            seen["retries"] = retries
            out = int(amount_in_wei / 1e8 * 61500 * 1e6)
            return {"amount_out": out, "price_impact": 0.008, "gas": 400000,
                    "swap_target": "0x" + "ac" * 20, "swap_calldata": "0xbeef"}
        ex.quote = q
        t = _target(1.0, 61000.0)
        ex.evaluate(None, t, gas_usd=0.01)                    # no deadline_mono -> classic
        self.assertEqual(seen["timeout"], ex.QUOTE_TIMEOUT)
        self.assertEqual(seen["retries"], ex.QUOTE_RETRIES)

    def test_no_quote_started_when_budget_below_floor(self):
        calls = {"n": 0}

        def q(*a, **k):
            calls["n"] += 1
            raise AssertionError("must not start a quote below the min-timeout floor")
        ex.quote = q
        t = _target(1.0, 61000.0)
        # budget already inside the floor -> stop before any network round-trip
        ev = ex.evaluate(None, t, gas_usd=0.01,
                         deadline_mono=time.monotonic() + ex.QUOTE_MIN_TIMEOUT / 2)
        self.assertIsNone(ev)
        self.assertEqual(calls["n"], 0)


class TestCalldata(unittest.TestCase):
    def test_selector_and_wellformed(self):
        t = _target()
        ev = {"repaid_shares": 232059231929812358, "seized_arg": 0,
              "swap_target": "0xAC4c6e212A361c968F1725b4d055b47E63F80b75",
              "swap_calldata": "0xdeadbeef", "min_profit_wei": 20000000}
        cd = ex.liquidate_calldata(t, ev)
        self.assertTrue(cd.startswith("0x79755efe"))     # verified vs cast sig
        self.assertEqual((len(cd) - 2) % 64, 8)          # selector(4B) + 32B words

    def test_capped_close_fires_seized_assets_mode(self):
        # M2: collateral-capped target (repaid < debt) must fire with seizedAssets pinned
        # (0.3% under the cap) and repaidShares == 0 — Morpho derives repaid at exec price
        ex.quote = _stub_quote(out_per_btc=61500, impact=0.008)
        t = _target(1.0, 61000.0)
        t["debt_assets"] = int(80000.0 * 1e6)            # debt > repaid -> capped
        ev = ex.evaluate(None, t, gas_usd=0.01)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["repaid_shares"], 0)
        self.assertEqual(ev["seized_arg"],
                         t["seized_assets"] * ex._HAIRCUT_NUM // ex._HAIRCUT_DEN)
        cd = ex.liquidate_calldata(t, {**ev, "min_profit_wei": 1})
        self.assertTrue(cd.startswith("0x79755efe"))

    def test_uncapped_close_fires_shares_mode(self):
        ex.quote = _stub_quote(out_per_btc=61500, impact=0.008)
        t = _target(1.0, 61000.0)                        # repaid == debt -> not capped
        ev = ex.evaluate(None, t, gas_usd=0.01)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["seized_arg"], 0)
        self.assertGreater(ev["repaid_shares"], 0)


class TestGuards(unittest.TestCase):
    def test_guard_trips_on_reverts(self):
        st = {"consec_reverts": ex.MAX_CONSEC_REVERTS, "gas_usd": 0.0}
        ok, _ = ex.guard_ok(st)
        self.assertFalse(ok)

    def test_guard_trips_on_daily_gas(self):
        st = {"consec_reverts": 0, "gas_usd": ex.MAX_DAILY_GAS_USD}
        ok, _ = ex.guard_ok(st)
        self.assertFalse(ok)

    def test_dedup_success_blocks_only_briefly(self):
        # H6: after a confirmed success the remainder must be re-takeable within seconds —
        # blocking (market,borrower) for 5min gifts the rest of a chunked close to competitors
        st = {"sent": {"k": {"ts": 1000.0, "status": "ok"}}}
        self.assertTrue(ex.recently_fired(st, "k", 1000.0 + ex.DEDUP_OK_SEC - 1))
        self.assertFalse(ex.recently_fired(st, "k", 1000.0 + ex.DEDUP_OK_SEC + 1))

    def test_dedup_blocks_while_pending(self):
        st = {"sent": {"k": {"ts": 1000.0, "status": "pending"}}}
        self.assertTrue(ex.recently_fired(st, "k", 1000.0 + ex.DEDUP_SEC - 1))
        self.assertFalse(ex.recently_fired(st, "k", 1000.0 + ex.DEDUP_SEC + 1))

    def test_dedup_allows_retry_after_revert(self):
        st = {"sent": {"k": {"ts": 1000.0, "status": "revert"}}}
        self.assertFalse(ex.recently_fired(st, "k", 1000.0 + 1))  # revert -> retry allowed

    def test_lost_race_classification(self):
        # H5: Morpho reverts Error("position is healthy") when beaten; Panic(0x11) on
        # over-repay after a competitor's partial close — neither is a bot defect
        self.assertTrue(ex._is_lost_race("execution reverted: position is healthy"))
        self.assertTrue(ex._is_lost_race(
            "rpc eth_call: {'code': 3, 'data': '0x4e487b71" + "0" * 62 + "11'}"))
        self.assertFalse(ex._is_lost_race("execution reverted: custom error 0x1234abcd"))
        self.assertFalse(ex._is_lost_race("SwapFailed()"))

    def test_lost_race_not_counted_as_revert(self):
        st = {"sent": {}, "consec_reverts": 0, "reverts": 0}
        ex._record(st, "k", "0xabc", 1000.0, "lost_race")
        self.assertEqual(st["consec_reverts"], 0)
        self.assertEqual(st["reverts"], 0)
        self.assertEqual(st["races_lost"], 1)
        ex._record(st, "k", "0xabc", 1000.0, "revert")
        self.assertEqual(st["consec_reverts"], 1)


class _StubConn:
    """Stands in for the kept-alive write connection: returns a canned HTTP body once."""
    def __init__(self, payload: bytes):
        self._payload = payload

    def request(self, *a, **k):
        pass

    def getresponse(self):
        payload = self._payload

        class R:
            def read(self):
                return payload
        return R()

    def close(self):
        pass


class TestRpcErrorClassification(unittest.TestCase):
    """Transport/rate-limit vs execution revert: only a GENUINE revert may ever decline a
    target — a rate-limited write RPC ({"code":-32005}) in a cascade used to be raised as the
    same bare RuntimeError and self-banned every profitable target for DECLINE_TTL."""

    def setUp(self):
        self._save = (ex._WRITE_URLS, ex._write_conn, ex._write_idx)
        # unreachable fallback (discard port): a reconnect attempt fails instantly, offline
        ex._WRITE_URLS = [urllib.parse.urlsplit("http://127.0.0.1:9")]
        ex._write_idx = 0

    def tearDown(self):
        (ex._WRITE_URLS, ex._write_conn, ex._write_idx) = self._save

    @staticmethod
    def _rpc_error(err: dict) -> bytes:
        return json.dumps({"jsonrpc": "2.0", "id": 1, "error": err}).encode()

    def test_is_revert_error(self):
        # genuine reverts: EIP-1474 code 3, legacy -32015, message/data-carried revert
        self.assertTrue(ex._is_revert_error(
            {"code": 3, "message": "execution reverted", "data": "0x08c379a0" + "00" * 32}))
        self.assertTrue(ex._is_revert_error({"code": -32015, "message": "vm execution error"}))
        self.assertTrue(ex._is_revert_error(
            {"code": -32000, "message": "execution reverted: position is healthy"}))
        self.assertTrue(ex._is_revert_error({"code": -32000, "data": "0x4e487b71" + "00" * 32}))
        # NOT reverts: rate limit, internal, send-path noise
        self.assertFalse(ex._is_revert_error({"code": -32005, "message": "limit exceeded"}))
        self.assertFalse(ex._is_revert_error({"code": -32603, "message": "internal error"}))
        self.assertFalse(ex._is_revert_error(
            {"code": -32000, "message": "insufficient funds for gas * price + value"}))
        self.assertFalse(ex._is_revert_error({"code": -32000, "message": "nonce too low"}))

    def test_rate_limit_raises_transport(self):
        ex._write_conn = _StubConn(self._rpc_error({"code": -32005, "message": "rate limit"}))
        with self.assertRaises(ex.RpcTransportError):
            ex._rpc_write("eth_call", [])

    def test_revert_raises_plain_runtime(self):
        ex._write_conn = _StubConn(self._rpc_error(
            {"code": 3, "message": "execution reverted: position is healthy"}))
        try:
            ex._rpc_write("eth_call", [])
            self.fail("expected a revert RuntimeError")
        except ex.RpcTransportError:
            self.fail("genuine revert misclassified as transport")
        except RuntimeError as e:
            self.assertIn("healthy", str(e))

    def test_garbage_body_is_transport_and_rotates(self):
        # rate-limit bodies often come as HTML/garbage — must be transport, and the endpoint
        # index must rotate to the next KT_WRITE_RPCS fallback
        ex._write_conn = _StubConn(b"<html>502 Bad Gateway</html>")
        with self.assertRaises(ex.RpcTransportError):
            ex._rpc_write("eth_call", [])
        self.assertGreater(ex._write_idx, 0)

    def test_preflight_propagates_transport_but_returns_revert(self):
        orig = ex._rpc_write
        try:
            def limited(method, params, timeout=15.0):
                raise ex.RpcTransportError("rpc eth_call: {'code': -32005}")
            ex._rpc_write = limited
            with self.assertRaises(ex.RpcTransportError):
                ex._preflight_call("0x79755efe")

            def reverted(method, params, timeout=15.0):
                raise RuntimeError("rpc eth_call: {'code': 3, 'message': "
                                   "'execution reverted: position is healthy'}")
            ex._rpc_write = reverted
            ok, why = ex._preflight_call("0x79755efe")
            self.assertFalse(ok)
            self.assertTrue(ex._is_lost_race(why))
        finally:
            ex._rpc_write = orig


class _StubRpc:
    """Oracle re-read stub for fire(): returns the scan price (no adverse move)."""
    def __init__(self, price):
        self._price = price

    def eth_call(self, to, data):
        return hex(self._price)


class TestFireTransportBackoff(unittest.TestCase):
    """fire() on a preflight transport failure: short TRANSIENT_BACKOFF_SEC backoff, NOT the
    60s DECLINE_TTL self-ban; a genuine preflight revert still declines for the full TTL."""

    def setUp(self):
        self._save = (ex.DRY_RUN, ex.quote, ex._preflight_call, ex.alert)
        ex.DRY_RUN = False
        ex.quote = _stub_quote(out_per_btc=61500, impact=0.008)
        ex.alert = lambda text, sync=False: None

    def tearDown(self):
        (ex.DRY_RUN, ex.quote, ex._preflight_call, ex.alert) = self._save

    def _fresh(self):
        t = _target(1.0, 61000.0)
        ev = ex.evaluate(None, t, gas_usd=0.01)
        st = {"sent": {}, "declined": {}, "fires": 0, "gas_usd": 0.0,
              "consec_reverts": 0, "reverts": 0}
        return t, ev, st, f"{t['market_id']}:{t['borrower']}"

    def test_transport_backs_off_briefly_not_60s(self):
        t, ev, st, key = self._fresh()

        def boom(calldata):
            raise ex.RpcTransportError("rpc eth_call: {'code': -32005, 'message': 'rate limit'}")
        ex._preflight_call = boom
        ex.fire(_StubRpc(t["price"]), t, ev, st, 1000.0, 0.01)
        self.assertEqual(st["declined"][key].get("ttl"), ex.TRANSIENT_BACKOFF_SEC)
        self.assertTrue(ex.recently_declined(st, key, 1000.0 + ex.TRANSIENT_BACKOFF_SEC - 0.5))
        self.assertFalse(ex.recently_declined(st, key, 1000.0 + ex.TRANSIENT_BACKOFF_SEC + 0.5))
        self.assertEqual(st["fires"], 0)         # never sent, never charged
        self.assertEqual(st["gas_usd"], 0.0)
        self.assertEqual(st["consec_reverts"], 0)

    def test_true_preflight_revert_declines_full_ttl(self):
        t, ev, st, key = self._fresh()
        ex._preflight_call = lambda calldata: (False, "execution reverted: SwapFailed()")
        ex.fire(_StubRpc(t["price"]), t, ev, st, 1000.0, 0.01)
        self.assertNotIn("ttl", st["declined"][key])
        self.assertTrue(ex.recently_declined(st, key, 1000.0 + ex.DECLINE_TTL - 1))
        self.assertFalse(ex.recently_declined(st, key, 1000.0 + ex.DECLINE_TTL + 1))

    def test_lost_race_preflight_still_counted(self):
        t, ev, st, key = self._fresh()
        ex._preflight_call = lambda calldata: (False, "execution reverted: position is healthy")
        ex.fire(_StubRpc(t["price"]), t, ev, st, 1000.0, 0.01)
        self.assertEqual(st.get("races_lost"), 1)
        self.assertIn(key, st["declined"])


class TestSendErrorHandling(unittest.TestCase):
    """Send errors (insufficient funds / RPC down mid-send) must cool the target down and
    throttle alerts — an unfunded wallet used to re-quote + re-fire + TG-alert every ~1s."""

    def test_send_error_cooldown(self):
        st = {"sent": {"k": {"ts": 1000.0, "status": "send_error"}}}
        self.assertTrue(ex.recently_fired(st, "k", 1000.0 + ex.SEND_ERR_COOLDOWN_SEC - 1))
        self.assertFalse(ex.recently_fired(st, "k", 1000.0 + ex.SEND_ERR_COOLDOWN_SEC + 1))

    def test_alert_throttled_per_target_and_globally(self):
        alerts = []
        save = ex.alert
        ex.alert = lambda text, sync=False: alerts.append(text)
        try:
            st = {"sent": {}, "gas_usd": 0.05}
            ex._record_send_error(st, "a", 1000.0, 0.01, "insufficient funds", "send")
            self.assertEqual(len(alerts), 1)
            self.assertEqual(st["sent"]["a"]["status"], "send_error")
            self.assertAlmostEqual(st["gas_usd"], 0.04)          # gas estimate refunded
            # same target again inside SEND_ERR_ALERT_SEC -> throttled
            ex._record_send_error(st, "a", 1031.0, 0.01, "insufficient funds", "send")
            self.assertEqual(len(alerts), 1)
            # ANOTHER target inside the global window -> throttled too (cascade = N targets)
            ex._record_send_error(st, "b", 1032.0, 0.01, "insufficient funds", "send")
            self.assertEqual(len(alerts), 1)
            # another target past the global window -> alerts
            ex._record_send_error(st, "b", 1000.0 + ex.SEND_ERR_ALERT_GLOBAL_SEC + 33,
                                  0.01, "insufficient funds", "send")
            self.assertEqual(len(alerts), 2)
            # same target past its per-target window -> alerts again
            ex._record_send_error(st, "a", 1000.0 + ex.SEND_ERR_ALERT_SEC + 200,
                                  0.01, "insufficient funds", "send")
            self.assertEqual(len(alerts), 3)
        finally:
            ex.alert = save


class TestBalanceCheck(unittest.TestCase):
    """EOA gas-balance guard: the node needs balance >= GAS_LIMIT*maxFeePerGas (the FULL fee
    envelope — ~1.08 ETH at the 600 gwei bid cap), so 'fund $50-100' can never fire a bid."""

    def setUp(self):
        self._save = (ex.FEE_BID, ex.GAS_LIMIT, ex.GAS_UNITS_EST, ex.MAX_PRIORITY_GWEI,
                      ex.PRIORITY_GWEI, ex.BALANCE_FIRES, ex._owner_addr_cache,
                      ex._rpc_write, ex.alert, ex._last_balance_check)
        ex.GAS_LIMIT, ex.GAS_UNITS_EST = 1_800_000, 900_000
        ex.MAX_PRIORITY_GWEI, ex.PRIORITY_GWEI, ex.BALANCE_FIRES = 600.0, 0.001, 3
        ex._owner_addr_cache = "0x" + "11" * 20
        ex._last_balance_check = 0.0
        self.alerts = []
        ex.alert = lambda text, sync=False: self.alerts.append(text)
        self.balance = 0

        def rpc(method, params, timeout=15.0):
            if method == "eth_getBalance":
                return hex(self.balance)
            if method == "eth_getBlockByNumber":
                return {"baseFeePerGas": hex(1_000_000)}   # Katana base ~0.001 gwei
            raise AssertionError(f"unexpected write call {method}")
        ex._rpc_write = rpc

    def tearDown(self):
        (ex.FEE_BID, ex.GAS_LIMIT, ex.GAS_UNITS_EST, ex.MAX_PRIORITY_GWEI,
         ex.PRIORITY_GWEI, ex.BALANCE_FIRES, ex._owner_addr_cache,
         ex._rpc_write, ex.alert, ex._last_balance_check) = self._save

    def test_fee_bid_needs_full_envelope(self):
        # bid cap 600 gwei -> envelope GAS_LIMIT*(2*base+cap) ≈ 1.08 ETH (STATE.md table);
        # a '$50-100' funding (~0.03-0.05 ETH) is 10-40x short and must alert
        ex.FEE_BID = True
        self.balance = int(0.5e18)
        self.assertFalse(ex.check_balance({}, 1e9, force=True))
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("LOW GAS BALANCE", self.alerts[0])
        self.balance = int(1.2e18)                       # above the 1.08 ETH envelope
        self.assertTrue(ex.check_balance({}, 1e9, force=True))
        self.assertEqual(len(self.alerts), 1)

    def test_default_tip_floor_is_tiny(self):
        # fee-bidding off: floor = K fires at the default tip ≈ 5.4e12 wei — 0.01 ETH passes
        ex.FEE_BID = False
        self.balance = int(1e16)
        self.assertTrue(ex.check_balance({}, 1e9, force=True))
        self.assertEqual(self.alerts, [])

    def test_low_balance_alert_throttled(self):
        ex.FEE_BID = True
        self.balance = 0
        st = {}
        self.assertFalse(ex.check_balance(st, 1e9, force=True))
        self.assertFalse(ex.check_balance(st, 1e9 + 60, force=True))     # < BALANCE_ALERT_SEC
        self.assertEqual(len(self.alerts), 1)                            # throttled
        self.assertFalse(ex.check_balance(st, 1e9 + ex.BALANCE_ALERT_SEC + 1, force=True))
        self.assertEqual(len(self.alerts), 2)

    def test_periodic_check_gated_by_cadence(self):
        ex.FEE_BID = True
        self.balance = 0
        st = {}
        self.assertFalse(ex.check_balance(st, 1e9))          # first periodic check runs
        self.assertTrue(ex.check_balance(st, 1e9 + 1))       # inside BALANCE_CHECK_SEC -> skip
        self.assertEqual(len(self.alerts), 1)

    def test_no_key_no_check(self):
        ex._owner_addr_cache = ""                            # no derivable EOA -> skip silently
        self.assertTrue(ex.check_balance({}, 1e9, force=True))
        self.assertEqual(self.alerts, [])


class _ArmRpc:
    """Read-RPC stub for the arm path: position() read for live shares + gas price."""
    def __init__(self, borrow_shares=int(61000e6 * 1000)):
        self._pos = "0x" + "0" * 64 + f"{borrow_shares:064x}" + f"{int(1e8):064x}"

    def eth_call(self, to, data, tag="latest", gas=None):
        return self._pos

    def gas_price(self):
        return int(0.001e9)


def _hot_row(hf=1.0005, debt_usd=61000.0):
    t = _target(1.0, 61000.0)
    t["hf"] = hf
    t["debt_usd"] = debt_usd
    t["collateral"] = int(1.2e8)     # 1.2 vbWBTC backing the ~1 BTC full-close seize
    return t


class TestArmCandidates(unittest.TestCase):
    def test_window_gate_and_order(self):
        rows = [_hot_row(0.998, 9000),      # already flipped — classic path's job
                _hot_row(1.0005, 2000), _hot_row(1.0015, 8000),
                _hot_row(1.0019, None),     # unknown USD — still watched (like once())
                _hot_row(1.0005, 100),      # below MIN_DEBT gate
                _hot_row(1.01, 50000)]      # outside KT_ARM_HF
        cands = ex._arm_candidates(rows)
        self.assertEqual([r["debt_usd"] for r in cands], [8000, 2000, None])

    def test_cap_biggest_first(self):
        save = ex.ARM_MAX_N
        ex.ARM_MAX_N = 2
        try:
            rows = [_hot_row(1.001, d) for d in (1000, 3000, 2000)]
            self.assertEqual([r["debt_usd"] for r in ex._arm_candidates(rows)], [3000, 2000])
        finally:
            ex.ARM_MAX_N = save


class TestArmRefresh(unittest.TestCase):
    """Idle-zone arming: quotes/thresholds cached, blind-fire gating, sanity preflight."""

    def setUp(self):
        self._save = (ex.DRY_RUN, ex.quote, ex.FEE_BID, ex.BLIND_FIRE, ex.PRIVATE_KEY,
                      ex._preflight_call, ex._rpc_write, ex.alert, ex.fire)
        ex._arm.clear()
        ex.DRY_RUN = True
        ex.BLIND_FIRE = True
        ex.FEE_BID = False
        ex.quote = _stub_quote(out_per_btc=61500, impact=0.008)
        ex.alert = lambda text, sync=False: None

    def tearDown(self):
        (ex.DRY_RUN, ex.quote, ex.FEE_BID, ex.BLIND_FIRE, ex.PRIVATE_KEY,
         ex._preflight_call, ex._rpc_write, ex.alert, ex.fire) = self._save
        ex._arm.clear()

    def _st(self):
        return {"sent": {}, "declined": {}, "fires": 0, "gas_usd": 0.0,
                "consec_reverts": 0, "reverts": 0}

    def test_dry_run_arms_quote_without_signing(self):
        row = _hot_row()
        ex._arm_refresh(_ArmRpc(), [row], self._st(), 1000.0, time.monotonic() + 5)
        key = f"{row['market_id']}:{row['borrower']}"
        e = ex._arm[key]
        self.assertIsNotNone(e["ev"])
        self.assertTrue(e["calldata"].startswith("0x79755efe"))
        self.assertIsNone(e["raw"])                       # DRY_RUN: nothing signed
        self.assertTrue(e["blind"])                       # default tip -> blind allowed
        self.assertEqual(e["ev"]["repaid_shares"], int(61000e6 * 1000))  # LIVE shares used

    def test_unprofitable_candidate_skipped_with_ttl(self):
        ex.quote = _stub_quote(out_per_btc=50000, impact=0.005)   # below repaid at any size
        row = _hot_row()
        st = self._st()
        ex._arm_refresh(_ArmRpc(), [row], st, 1000.0, time.monotonic() + 5)
        key = f"{row['market_id']}:{row['borrower']}"
        self.assertGreater(ex._arm[key]["skip_until"], 1000.0)
        self.assertNotIn(key, st["declined"])             # NOT the classic decline journal

    def test_budget_exhausted_stops_before_quoting(self):
        calls = {"n": 0}

        def q(*a, **k):
            calls["n"] += 1
            raise AssertionError("must not quote past the idle budget")
        ex.quote = q
        ex._arm_refresh(_ArmRpc(), [_hot_row()], self._st(), 1000.0,
                        time.monotonic() - 1)             # budget already exhausted
        self.assertEqual(calls["n"], 0)
        self.assertEqual(ex._arm, {})

    def test_evaluate_deadline_cap_blocks_further_quotes(self):
        # arm path: a budget below one quote's floor must never start a Sushi round-trip that
        # can't finish inside the armed window (the small fix — a 5ms budget can't fit a quote)
        calls = {"n": 0}

        def q(token_in, token_out, amount_in_wei, sender, recipient, max_slippage=0.005, **kw):
            calls["n"] += 1
            time.sleep(0.01)                              # simulate quote latency
            out = int(amount_in_wei / 1e8 * 50000 * 1e6)  # unprofitable at every size
            return {"amount_out": out, "price_impact": 0.005, "gas": 400000,
                    "swap_target": "0x" + "ac" * 20, "swap_calldata": "0xbeef"}
        ex.quote = q
        t = _target(1.0, 61000.0)
        ev = ex.evaluate(None, t, gas_usd=0.01, deadline_mono=time.monotonic() + 0.005)
        self.assertIsNone(ev)
        self.assertEqual(calls["n"], 0)                   # sub-floor budget -> never started

    def test_budget_stopped_evaluate_is_not_a_decline(self):
        # evaluate returning None because the BUDGET ran out must NOT skip the target for
        # DECLINE_TTL — economics were never judged; the next idle window retries
        def q(token_in, token_out, amount_in_wei, sender, recipient, max_slippage=0.005, **kw):
            time.sleep(0.01)
            out = int(amount_in_wei / 1e8 * 50000 * 1e6)
            return {"amount_out": out, "price_impact": 0.005, "gas": 400000,
                    "swap_target": "0x" + "ac" * 20, "swap_calldata": "0xbeef"}
        ex.quote = q
        row = _hot_row()
        ex._arm_refresh(_ArmRpc(), [row], self._st(), 1000.0, time.monotonic() + 0.005)
        self.assertNotIn(f"{row['market_id']}:{row['borrower']}", ex._arm)

    def test_fresh_entry_not_requoted(self):
        row = _hot_row()
        st = self._st()
        ex._arm_refresh(_ArmRpc(), [row], st, 1000.0, time.monotonic() + 5)
        calls = {"n": 0}

        def q(*a, **k):
            calls["n"] += 1
            raise AssertionError("fresh entry must not re-quote")
        ex.quote = q
        ex._arm_refresh(_ArmRpc(), [row], st, 1001.0, time.monotonic() + 5)
        self.assertEqual(calls["n"], 0)

    def test_live_arming_signs_after_healthy_preflight(self):
        ex.DRY_RUN = False
        ex.PRIVATE_KEY = "0x" + "01" * 32
        ex._preflight_call = lambda cd: (False, "execution reverted: position is healthy")
        ex._rpc_write = lambda m, p, timeout=15.0: (
            {"baseFeePerGas": hex(1_000_000)} if m == "eth_getBlockByNumber" else "0x5")
        row = _hot_row()
        st = self._st()
        ex._arm_refresh(_ArmRpc(), [row], st, 1000.0, time.monotonic() + 5)
        e = ex._arm[f"{row['market_id']}:{row['borrower']}"]
        self.assertTrue(str(e["raw"]).startswith("0x"))   # pre-signed, nonce-frozen
        self.assertEqual(e["fires_at_sign"], 0)
        self.assertTrue(e["blind"])

    def test_unexpected_preflight_revert_never_arms(self):
        # arm-time preflight of a HEALTHY target must revert 'position is healthy'; anything
        # else means broken calldata — blind-firing it would burn a fire on garbage
        ex.DRY_RUN = False
        ex.PRIVATE_KEY = "0x" + "01" * 32
        ex._preflight_call = lambda cd: (False, "execution reverted: SwapFailed()")
        row = _hot_row()
        ex._arm_refresh(_ArmRpc(), [row], self._st(), 1000.0, time.monotonic() + 5)
        e = ex._arm[f"{row['market_id']}:{row['borrower']}"]
        self.assertNotIn("raw", e)
        self.assertGreater(e["skip_until"], 1000.0)

    def test_already_flipped_fires_classic_immediately(self):
        ex.DRY_RUN = False
        ex.PRIVATE_KEY = "0x" + "01" * 32
        ex._preflight_call = lambda cd: (True, "")        # liquidatable RIGHT NOW
        fired = []
        ex.fire = lambda rpc, t, ev, st, now_ts, gas_usd: fired.append(t["borrower"])
        row = _hot_row()
        ex._arm_refresh(_ArmRpc(), [row], self._st(), 1000.0, time.monotonic() + 5)
        self.assertEqual(fired, [row["borrower"]])
        self.assertNotIn(f"{row['market_id']}:{row['borrower']}", ex._arm)

    def test_fee_bid_target_is_never_blind(self):
        # Phase 2 engaged: a reverted bid burns the bid, so the preflight eth_call must stay
        # in the critical path — the entry arms, but with blind=False and the bid's gas cost
        ex.DRY_RUN = False
        ex.PRIVATE_KEY = "0x" + "01" * 32
        ex.FEE_BID = True
        save = (ex.FEE_BID_MIN_NET_USD, ex.ETH_USD)
        ex.FEE_BID_MIN_NET_USD, ex.ETH_USD = 100.0, 1900.0
        ex._preflight_call = lambda cd: (False, "execution reverted: position is healthy")
        ex._rpc_write = lambda m, p, timeout=15.0: (
            {"baseFeePerGas": hex(1_000_000)} if m == "eth_getBlockByNumber" else "0x5")
        try:
            row = _hot_row()
            ex._arm_refresh(_ArmRpc(), [row], self._st(), 1000.0, time.monotonic() + 5)
            e = ex._arm[f"{row['market_id']}:{row['borrower']}"]
            self.assertGreater(e["bid_gwei"], ex.PRIORITY_GWEI)
            self.assertFalse(e["blind"])
            self.assertGreater(e["gas_usd"], 1.0)         # bid win-cost, not the base est
        finally:
            (ex.FEE_BID_MIN_NET_USD, ex.ETH_USD) = save

    def test_preflight_transport_arms_without_blind(self):
        ex.DRY_RUN = False
        ex.PRIVATE_KEY = "0x" + "01" * 32

        def boom(cd):
            raise ex.RpcTransportError("rate limit")
        ex._preflight_call = boom
        ex._rpc_write = lambda m, p, timeout=15.0: (
            {"baseFeePerGas": hex(1_000_000)} if m == "eth_getBlockByNumber" else "0x5")
        row = _hot_row()
        ex._arm_refresh(_ArmRpc(), [row], self._st(), 1000.0, time.monotonic() + 5)
        e = ex._arm[f"{row['market_id']}:{row['borrower']}"]
        self.assertFalse(e["blind"])                      # sanity check blinded -> preflight
        self.assertTrue(str(e["raw"]).startswith("0x"))   # ... but still pre-signed


class TestFireFast(unittest.TestCase):
    """Armed-window critical path: blind send, bid-preflight gating, nonce-burn guard."""

    def setUp(self):
        self._save = (ex.DRY_RUN, ex._rpc_write, ex._post_broadcast, ex._preflight_call,
                      ex.alert)
        ex.DRY_RUN = False
        ex.alert = lambda text, sync=False: None
        self.sent = []
        self.tracked = []

        def rpc_write(method, params, timeout=15.0):
            assert method == "eth_sendRawTransaction", f"unexpected {method} in critical path"
            self.sent.append(params[0])
            return "0x" + "ab" * 32
        ex._rpc_write = rpc_write
        ex._post_broadcast = lambda *a: self.tracked.append(a)

    def tearDown(self):
        (ex.DRY_RUN, ex._rpc_write, ex._post_broadcast, ex._preflight_call,
         ex.alert) = self._save

    def _entry(self, blind=True, raw="0xf86b...", age=0.0):
        t = _hot_row()
        ev = {"f": 1.0, "net_usd": 500.0, "net_wei": int(500e6), "repaid_shares": 1,
              "seized_arg": 0}
        return {"t": t, "ev": ev, "key": f"{t['market_id']}:{t['borrower']}",
                "calldata": "0x79755efe", "raw": raw, "blind": blind, "bid_gwei": 0.001,
                "gas_usd": 0.01, "ts": time.monotonic() - age, "fires_at_sign": 0}

    def _st(self):
        return {"sent": {}, "declined": {}, "fires": 0, "gas_usd": 0.0,
                "consec_reverts": 0, "reverts": 0}

    def test_blind_sends_presigned_with_zero_extra_rpc(self):
        st = self._st()
        e = self._entry()
        self.assertTrue(ex._fire_fast(e, st, 1000.0))
        self.assertEqual(self.sent, ["0xf86b..."])        # the pre-signed raw, nothing else
        self.assertEqual(st["fires"], 1)
        self.assertAlmostEqual(st["gas_usd"], 0.01)
        self.assertIsNone(e["raw"])                       # nonce consumed
        self.assertEqual(len(self.tracked), 1)            # settle handed to shared tracking

    def test_send_failure_refunds_and_leaves_no_cooldown(self):
        def boom(method, params, timeout=15.0):
            raise ex.RpcTransportError("write RPC down")
        ex._rpc_write = boom
        st = self._st()
        e = self._entry()
        self.assertFalse(ex._fire_fast(e, st, 1000.0))
        self.assertEqual(st["fires"], 0)                  # refunded — classic path untainted
        self.assertEqual(st["gas_usd"], 0.0)
        self.assertEqual(st["sent"], {})                  # NO send_error cooldown on the key
        self.assertIsNone(e["raw"])

    def test_nonce_burned_since_signing_blocks_blind(self):
        st = self._st()
        st["fires"] = 2                                   # someone fired after we signed
        self.assertFalse(ex._fire_fast(self._entry(), st, 1000.0))
        self.assertEqual(self.sent, [])

    def test_stale_entry_never_fires(self):
        st = self._st()
        self.assertFalse(ex._fire_fast(self._entry(age=ex.ARM_QUOTE_TTL + 1), st, 1000.0))
        self.assertEqual(self.sent, [])

    def test_dry_run_prints_never_sends(self):
        ex.DRY_RUN = True
        self.assertFalse(ex._fire_fast(self._entry(), self._st(), 1000.0))
        self.assertEqual(self.sent, [])

    def test_dedup_respected(self):
        st = self._st()
        e = self._entry()
        st["sent"][e["key"]] = {"ts": 999.0, "status": "pending"}
        self.assertFalse(ex._fire_fast(e, st, 1000.0))
        self.assertEqual(self.sent, [])

    def test_guard_trips_before_send(self):
        st = self._st()
        st["consec_reverts"] = ex.MAX_CONSEC_REVERTS
        with self.assertRaises(ex.GuardTripped):
            ex._fire_fast(self._entry(), st, 1000.0)
        self.assertEqual(self.sent, [])

    def test_bid_entry_preflights_then_sends(self):
        st = self._st()
        pf = []
        ex._preflight_call = lambda cd: (pf.append(cd) or (True, ""))
        self.assertTrue(ex._fire_fast(self._entry(blind=False), st, 1000.0))
        self.assertEqual(pf, ["0x79755efe"])              # preflight stayed in the path
        self.assertEqual(len(self.sent), 1)

    def test_bid_entry_lost_race_declines_not_sends(self):
        st = self._st()
        ex._preflight_call = lambda cd: (False, "execution reverted: position is healthy")
        e = self._entry(blind=False)
        self.assertFalse(ex._fire_fast(e, st, 1000.0))
        self.assertEqual(self.sent, [])
        self.assertEqual(st["races_lost"], 1)
        self.assertIn(e["key"], st["declined"])

    def test_bid_entry_preflight_transport_short_backoff(self):
        st = self._st()

        def boom(cd):
            raise ex.RpcTransportError("rate limit")
        ex._preflight_call = boom
        e = self._entry(blind=False)
        self.assertFalse(ex._fire_fast(e, st, 1000.0))
        self.assertEqual(st["declined"][e["key"]].get("ttl"), ex.TRANSIENT_BACKOFF_SEC)


class _FakeClock:
    """Stands in for fastpath.BlockClock inside _predictive_cycle. When wait_next yields no
    detect, `synced_after` mimics soft (predicted anchor) vs hard (pattern broke) breaks."""
    def __init__(self, synced=True, next_block=(101, 0.0), synced_after=False):
        self._synced, self._next, self._after = synced, next_block, synced_after

    @property
    def synced(self):
        return self._synced

    def sync(self):
        return None

    def idle_remaining(self):
        return 0.5

    def wait_next(self):
        if self._next is None:
            self._synced = self._after
        return self._next


class _CycleRpc:
    def __init__(self, ret_hex):
        self.ret = ret_hex
        self.calls = []

    def warm(self, timeout=2.0):
        return True

    def eth_call(self, to, data, tag="latest", gas=None):
        self.calls.append((to, data))
        return self.ret


class TestPredictiveCycle(unittest.TestCase):
    def setUp(self):
        self._save = (ex._arm_refresh, ex._fire_fast, ex._warm_write, ex.save_state)
        ex._arm.clear()
        ex._arm_refresh = lambda *a, **k: None
        ex._warm_write = lambda: None
        ex.save_state = lambda st: None

    def tearDown(self):
        (ex._arm_refresh, ex._fire_fast, ex._warm_write, ex.save_state) = self._save
        ex._arm.clear()

    def test_flip_fires_armed_entry(self):
        from bot.test_fastpath import _agg3_return
        row = _hot_row()                                  # HF just above 1
        key = f"{row['market_id']}:{row['borrower']}"
        ex._arm[key] = {"ev": {"x": 1}, "key": key}
        fired = []
        ex._fire_fast = lambda entry, st, now_ts: fired.append(entry["key"]) or True
        # fresh price ONE wei below the flip threshold -> flipped
        import bot.fastpath as fp
        flip_px = fp.min_healthy_price(row["collateral"], row["lltv"], row["debt_assets"])
        rpc = _CycleRpc(_agg3_return([(True, (flip_px - 1).to_bytes(32, "big"))]))
        st = {"sent": {}, "declined": {}, "fires": 0, "gas_usd": 0.0, "consec_reverts": 0}
        self.assertTrue(ex._predictive_cycle(rpc, _FakeClock(), [row], st))
        self.assertEqual(fired, [key])
        self.assertEqual(len(rpc.calls), 1)               # ONE multicall in the armed window
        self.assertEqual(rpc.calls[0][0], ex.MULTICALL3)

    def test_no_flip_no_fire(self):
        from bot.test_fastpath import _agg3_return
        row = _hot_row()
        import bot.fastpath as fp
        flip_px = fp.min_healthy_price(row["collateral"], row["lltv"], row["debt_assets"])
        rpc = _CycleRpc(_agg3_return([(True, flip_px.to_bytes(32, "big"))]))  # AT threshold
        fired = []
        ex._fire_fast = lambda entry, st, now_ts: fired.append(1) or True
        st = {"sent": {}, "declined": {}, "fires": 0, "gas_usd": 0.0, "consec_reverts": 0}
        self.assertTrue(ex._predictive_cycle(rpc, _FakeClock(), [row], st))
        self.assertEqual(fired, [])

    def test_hard_break_falls_back_before_any_read(self):
        rpc = _CycleRpc("0x")
        st = {"sent": {}, "declined": {}}
        clock = _FakeClock(next_block=None, synced_after=False)
        self.assertFalse(ex._predictive_cycle(rpc, clock, [_hot_row()], st))
        self.assertEqual(rpc.calls, [])                   # no armed-window read was wasted

    def test_soft_break_stays_block_locked(self):
        # late armed-zone entry absorbed by a predicted anchor: no fire window this block,
        # but the loop must run the hot pass immediately (True), not sleep the classic poll
        rpc = _CycleRpc("0x")
        st = {"sent": {}, "declined": {}}
        clock = _FakeClock(next_block=None, synced_after=True)
        self.assertTrue(ex._predictive_cycle(rpc, clock, [_hot_row()], st))
        self.assertEqual(rpc.calls, [])


class TestFeeBid(unittest.TestCase):
    """Phase 2 competitive priority-fee bidding (_competitive_priority_gwei)."""
    def setUp(self):
        self._save = (ex.FEE_BID, ex.GAS_UNITS_EST, ex.ETH_USD, ex.MAX_PRIORITY_GWEI,
                      ex.FEE_BID_MIN_NET_USD, ex.FEE_BID_KEEP_USD, ex.PRIORITY_GWEI)
        ex.GAS_UNITS_EST, ex.ETH_USD, ex.MAX_PRIORITY_GWEI = 900000, 1900.0, 600.0
        ex.FEE_BID_MIN_NET_USD, ex.FEE_BID_KEEP_USD, ex.PRIORITY_GWEI = 300.0, 50.0, 0.001

    def tearDown(self):
        (ex.FEE_BID, ex.GAS_UNITS_EST, ex.ETH_USD, ex.MAX_PRIORITY_GWEI,
         ex.FEE_BID_MIN_NET_USD, ex.FEE_BID_KEEP_USD, ex.PRIORITY_GWEI) = self._save

    def test_disabled_by_default(self):
        ex.FEE_BID = False
        self.assertEqual(ex._competitive_priority_gwei(50000), ex.PRIORITY_GWEI)

    def test_no_bid_below_min_net(self):
        ex.FEE_BID = True
        self.assertEqual(ex._competitive_priority_gwei(250), ex.PRIORITY_GWEI)
        self.assertIsNone(ex._competitive_priority_gwei(None) and None)  # None net -> default

    def test_bid_is_capped(self):
        ex.FEE_BID = True
        self.assertEqual(ex._competitive_priority_gwei(50000), 600.0)   # huge net -> cap

    def test_bid_keeps_floor(self):
        ex.FEE_BID = True
        g = ex._competitive_priority_gwei(800)                          # below cap -> margin-aware
        cost = ex.GAS_UNITS_EST * g / 1e9 * ex.ETH_USD
        self.assertLess(g, 600.0)
        self.assertGreaterEqual(800 - cost, ex.FEE_BID_KEEP_USD - 1)    # keeps ~>= FEE_BID_KEEP_USD
        self.assertLessEqual(800 - cost, ex.FEE_BID_KEEP_USD + 1)


if __name__ == "__main__":
    unittest.main()
