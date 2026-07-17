"""Offline tests for the predictive fast path (no network, no sleeps — injected clocks).

The flip threshold is load-bearing: it is the ONLY health check between the armed-window
price read and a blind eth_sendRawTransaction, so it must mirror Morpho.sol _isHealthy
bit-exactly (floor/ceil divisions included) at magnitudes far past float64."""
import os
import unittest

os.environ.setdefault("DRY_RUN", "1")

from analysis.models import ORACLE_PRICE_SCALE, WAD, morpho_health_factor  # noqa: E402
from bot import fastpath as fp  # noqa: E402


# --- flip thresholds -------------------------------------------------------------------
class TestMinHealthyPrice(unittest.TestCase):
    def _max_borrow(self, coll, px, lltv):
        # exact Morpho.sol _isHealthy: wMulDown(mulDivDown(coll, px, 1e36), lltv)
        return coll * px // ORACLE_PRICE_SCALE * lltv // WAD

    def test_exact_boundary_over_grid(self):
        # at the returned price the position must be healthy; one wei of price below, not —
        # for magnitudes including >2^53 where float math silently rounds
        colls = [1, 10 ** 8, 10 ** 18, 3 * 10 ** 18 + 7, 10 ** 24 + 13]
        lltvs = [int(0.385e18), int(0.625e18), int(0.86e18), int(0.915e18), int(0.98e18)]
        debts = [1, 10 ** 6 + 3, 10 ** 18 + 1, 10 ** 27 + 11]
        for coll in colls:
            for lltv in lltvs:
                for debt in debts:
                    px = fp.min_healthy_price(coll, lltv, debt)
                    self.assertGreaterEqual(self._max_borrow(coll, px, lltv), debt,
                                            f"healthy fails at threshold c={coll} d={debt}")
                    if px > 0:
                        self.assertLess(self._max_borrow(coll, px - 1, lltv), debt,
                                        f"not tight: c={coll} l={lltv} d={debt}")

    def test_matches_float_hf_at_sane_magnitudes(self):
        coll, lltv, debt = 100 * WAD, int(0.86e18), 150 * WAD
        px = fp.min_healthy_price(coll, lltv, debt)
        self.assertGreaterEqual(morpho_health_factor(coll, px, lltv, debt), 1.0)
        self.assertLess(morpho_health_factor(coll, px - 10 ** 20, lltv, debt), 1.0)

    def test_no_debt_never_flips(self):
        self.assertEqual(fp.min_healthy_price(10 ** 18, int(0.86e18), 0), 0)

    def test_unbacked_debt_always_flips(self):
        self.assertEqual(fp.min_healthy_price(0, int(0.86e18), 10 ** 6), fp.ALWAYS_FLIP)
        self.assertEqual(fp.min_healthy_price(10 ** 18, 0, 10 ** 6), fp.ALWAYS_FLIP)

    def test_attach_thresholds_skips_already_liquidatable(self):
        rows = [{"hf": 1.001, "collateral": 10 ** 18, "lltv": int(0.86e18),
                 "debt_assets": 10 ** 6, "oracle": "0xaa"},
                {"hf": 0.997, "collateral": 10 ** 18, "lltv": int(0.86e18),
                 "debt_assets": 10 ** 6, "oracle": "0xbb"}]
        watch = fp.attach_flip_thresholds(rows)
        self.assertEqual(len(watch), 1)                      # HF<1 rows are the classic pass's
        self.assertEqual(watch[0]["oracle"], "0xaa")
        self.assertGreater(watch[0]["flip_px"], 0)


# --- armed-window price refresh ---------------------------------------------------------
def _agg3_return(results: list[tuple[bool, bytes]]) -> str:
    """ABI-encode an aggregate3 Result[] return (mirrors what Multicall3 emits)."""
    def word(x: int) -> bytes:
        return x.to_bytes(32, "big")
    tuples = []
    for ok, data in results:
        padded = data + b"\x00" * ((32 - len(data) % 32) % 32)
        tuples.append(word(1 if ok else 0) + word(0x40) + word(len(data)) + padded)
    offs, cur = [], 32 * len(results)
    for t in tuples:
        offs.append(cur)
        cur += len(t)
    arr = word(len(results)) + b"".join(word(o) for o in offs) + b"".join(tuples)
    return "0x" + (word(0x20) + arr).hex()


class TestPriceRefresh(unittest.TestCase):
    ROWS = [{"oracle": "0x" + "aa" * 20}, {"oracle": "0x" + "bb" * 20},
            {"oracle": "0x" + "aa" * 20}]                    # aa shared by two markets

    def test_build_dedupes_oracles(self):
        calldata, oracles = fp.build_price_refresh(self.ROWS)
        self.assertEqual(oracles, ["0x" + "aa" * 20, "0x" + "bb" * 20])
        self.assertTrue(calldata.startswith(fp.SEL_PRICE[:2]))  # 0x-prefixed calldata
        # one aggregate3 with two inner price() calls: selector appears once per oracle
        self.assertEqual(calldata.count(fp.SEL_PRICE[2:]), 2)

    def test_decode_maps_prices_and_drops_failures(self):
        _, oracles = fp.build_price_refresh(self.ROWS)
        ret = _agg3_return([(True, (123 * 10 ** 30).to_bytes(32, "big")), (False, b"")])
        prices = fp.decode_price_refresh(ret, oracles)
        self.assertEqual(prices, {"0x" + "aa" * 20: 123 * 10 ** 30})  # bb absent, not 0

    def test_flipped_selects_and_orders_by_debt(self):
        rows = [{"oracle": "0xa", "flip_px": 100, "debt_usd": 1000},
                {"oracle": "0xb", "flip_px": 100, "debt_usd": 9000},
                {"oracle": "0xc", "flip_px": 100, "debt_usd": 500}]
        prices = {"0xa": 99, "0xb": 42, "0xc": 100}          # c NOT below threshold
        hits = fp.flipped(rows, prices)
        self.assertEqual([r["debt_usd"] for r in hits], [9000, 1000])

    def test_flipped_ignores_missing_price(self):
        rows = [{"oracle": "0xa", "flip_px": fp.ALWAYS_FLIP, "debt_usd": 1}]
        self.assertEqual(fp.flipped(rows, {}), [])


# --- block phase lock -------------------------------------------------------------------
class _FakeChain:
    """Deterministic clock + chain: blocks increment at fixed boundaries; poll() costs RTT."""

    def __init__(self, base_block=100, first_boundary=0.55, block_sec=1.0, rtt=0.02):
        self.t = 0.0
        self.base, self.b0, self.dt, self.rtt = base_block, first_boundary, block_sec, rtt
        self.sleeps: list[float] = []
        self.polls = 0
        self.fail_polls: set[int] = set()     # poll indices (1-based) that return None
        self.jump_at: float | None = None     # boundary time after which the height +1 extra

    def now(self) -> float:
        return self.t

    def sleep(self, d: float) -> None:
        self.sleeps.append(d)
        self.t += d

    def height(self, t: float) -> int:
        if t < self.b0:
            return self.base
        n = self.base + 1 + int((t - self.b0) / self.dt)
        if self.jump_at is not None and t >= self.jump_at:
            n += 1
        return n

    def poll(self):
        self.polls += 1
        self.t += self.rtt
        if self.polls in self.fail_polls:
            return None
        return self.height(self.t)


def _clock(chain: _FakeChain, **kw) -> fp.BlockClock:
    kw.setdefault("block_sec", chain.dt)
    return fp.BlockClock(chain.poll, now=chain.now, sleep=chain.sleep, **kw)


class TestBlockClock(unittest.TestCase):
    def test_sync_anchors_on_observed_transition(self):
        ch = _FakeChain()
        c = _clock(ch)
        got = c.sync()
        self.assertIsNotNone(got)
        bn, t0 = got
        self.assertEqual(bn, 101)
        # anchor error bounded by coarse cadence + RTT
        self.assertLessEqual(t0 - ch.b0, 3 * c.step + 2 * ch.rtt)
        self.assertTrue(c.synced)

    def test_wait_next_detects_within_step_of_boundary(self):
        ch = _FakeChain()
        c = _clock(ch)
        c.sync()
        polls_before = ch.polls
        got = c.wait_next()
        self.assertIsNotNone(got)
        bn, t1 = got
        self.assertEqual(bn, 102)
        boundary = ch.b0 + ch.dt                             # 1.55
        self.assertGreaterEqual(t1, boundary)
        self.assertLessEqual(t1 - boundary, c.step + 2 * ch.rtt)   # detect latency budget
        self.assertEqual(c.t0, t1)                           # re-anchored on own detect
        # the idle zone slept in ONE stretch — no polling before t0+window
        idle_sleep = ch.sleeps[-(ch.polls - polls_before)]
        self.assertGreater(idle_sleep, c.window / 2)

    def test_repeated_cycles_hold_lock_without_drift(self):
        ch = _FakeChain()
        c = _clock(ch)
        c.sync()
        for i in range(5):
            got = c.wait_next()
            self.assertIsNotNone(got, f"lost lock at cycle {i}")
            bn, t1 = got
            self.assertEqual(bn, 102 + i)
            boundary = ch.b0 + (i + 1) * ch.dt
            self.assertLessEqual(t1 - boundary, c.step + 2 * ch.rtt)

    def test_idle_remaining_is_the_maintenance_budget(self):
        ch = _FakeChain()
        c = _clock(ch)
        c.sync()
        budget = c.idle_remaining()
        self.assertGreater(budget, 0.0)
        self.assertLessEqual(budget, c.window)
        c.t0 = None
        self.assertEqual(c.idle_remaining(), 0.0)

    def test_unsynced_wait_returns_none(self):
        c = _clock(_FakeChain())
        self.assertIsNone(c.wait_next())

    def test_overrun_invalidates_phase(self):
        ch = _FakeChain()
        c = _clock(ch)
        c.sync()
        ch.t = c.t0 + c.block_sec + c.slack + 0.1            # a slow pass ate the whole period
        self.assertIsNone(c.wait_next())
        self.assertFalse(c.synced)

    def test_bad_anchor_resyncs_instead_of_guessing(self):
        ch = _FakeChain()
        c = _clock(ch)
        c.sync()
        c.t0 += 0.5                                          # corrupt anchor: prediction from
        self.assertIsNone(c.wait_next())                     # it would land in the future ->
        self.assertFalse(c.synced)                           # refuse to guess the boundary

    def test_late_entry_predicts_anchor_and_stays_locked(self):
        # a slow pass ate past the boundary: no fireable detect, but the ~1.000s cadence
        # lets the clock predict the anchor — no ~1-block re-sync cost
        ch = _FakeChain()
        c = _clock(ch)
        c.sync()
        t0 = c.t0
        ch.t = t0 + 1.1                                      # enter after the next boundary
        self.assertIsNone(c.wait_next())                     # too late to fire this block...
        self.assertTrue(c.synced)                            # ...but still block-locked
        self.assertAlmostEqual(c.t0, t0 + c.block_sec)       # predicted anchor
        got = c.wait_next()                                  # next cycle detects normally
        self.assertIsNotNone(got)
        boundary = ch.b0 + 2 * ch.dt
        self.assertLessEqual(got[1] - boundary, c.step + 2 * ch.rtt)

    def test_predicted_anchors_never_chain_past_two(self):
        ch = _FakeChain()
        c = _clock(ch)
        c.sync()
        for i in range(2):                                   # two late entries absorbed
            ch.t = c.t0 + 1.1
            self.assertIsNone(c.wait_next())
            self.assertTrue(c.synced, f"lost lock on predicted anchor {i}")
        ch.t = c.t0 + 1.1                                    # third in a row: no observed
        self.assertIsNone(c.wait_next())                     # boundary for 3 blocks — the
        self.assertFalse(c.synced)                           # phase is guesswork, re-sync

    def test_skipped_block_resyncs(self):
        ch = _FakeChain()
        c = _clock(ch)
        c.sync()
        ch.jump_at = ch.b0 + ch.dt                           # next boundary bumps height by 2
        self.assertIsNone(c.wait_next())
        self.assertFalse(c.synced)

    def test_rpc_dead_during_armed_window_gives_up_at_slack(self):
        ch = _FakeChain()
        c = _clock(ch)
        c.sync()
        ch.fail_polls = set(range(ch.polls + 1, ch.polls + 10_000))
        self.assertIsNone(c.wait_next())
        self.assertFalse(c.synced)
        self.assertLessEqual(ch.t, c.block_sec + c.slack + ch.b0 + 3 * c.step + 1)

    def test_transient_poll_failures_tolerated(self):
        ch = _FakeChain()
        c = _clock(ch)
        c.sync()
        ch.fail_polls = {ch.polls + 1, ch.polls + 2}         # two Nones, then recovery
        got = c.wait_next()
        self.assertIsNotNone(got)
        self.assertEqual(got[0], 102)

    def test_sync_gives_up_when_rpc_never_ticks(self):
        ch = _FakeChain()
        ch.fail_polls = set(range(1, 10_000))
        c = _clock(ch)
        self.assertIsNone(c.sync())
        self.assertFalse(c.synced)


if __name__ == "__main__":
    unittest.main()
