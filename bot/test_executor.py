"""Offline tests for the executor's pure logic (no RPC, no network). The Sushi quote is stubbed
so chunk selection, the profit gate, and calldata encoding are tested deterministically."""
import os
import unittest

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
            "borrow_shares_repaid": int(repaid_usdc * 1e6 * 1000)}


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


class TestCalldata(unittest.TestCase):
    def test_selector_and_wellformed(self):
        t = _target()
        ev = {"repaid_shares": 232059231929812358,
              "swap_target": "0xAC4c6e212A361c968F1725b4d055b47E63F80b75",
              "swap_calldata": "0xdeadbeef", "min_profit_wei": 20000000}
        cd = ex.liquidate_calldata(t, ev)
        self.assertTrue(cd.startswith("0x4bffc045"))     # verified vs cast
        self.assertEqual((len(cd) - 2) % 64, 8)          # selector(4B) + 32B words


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


if __name__ == "__main__":
    unittest.main()
