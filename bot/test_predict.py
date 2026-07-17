"""Offline tests for the oracle-push prediction engine + driver (pure, no network/threads).

Covers the anchor/return/arm/disarm hysteresis state machine, anchor reset on a confirmed push,
lead-time + false-positive classification, the greppable PREDICT log grammar, and the driver
wiring (poll->push via updatedAt, lazy bootstrap, armed-set publish on change, poll-failure
isolation). CRUCIAL: asserts the layer NEVER fires — a prediction with no confirmed push produces
no 'confirmed' event and the driver has no send path at all."""
import os
import unittest

os.environ.setdefault("DRY_RUN", "1")

from bot import predict as pr                       # noqa: E402
from bot import oracles as oc                        # noqa: E402


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t


def _engine(arm=0.0045, disarm=0.0035, window=90.0, clock=None):
    clock = clock or _Clock()
    return pr.PredictEngine(("BTCUSDT", "ETHUSDT"), arm_pct=arm, disarm_pct=disarm,
                            falsepos_window=window, now=clock.now), clock


class TestEngineAnchorReturn(unittest.TestCase):
    def test_bootstrap_sets_anchor_once(self):
        e, _ = _engine()
        evs = e.bootstrap("BTCUSDT", 63000.0)
        self.assertEqual(evs[0]["event"], "bootstrap")
        self.assertEqual(e.feeds["BTCUSDT"].anchor, 63000.0)
        self.assertEqual(e.bootstrap("BTCUSDT", 999.0), [])    # idempotent — anchor unchanged
        self.assertEqual(e.feeds["BTCUSDT"].anchor, 63000.0)

    def test_bootstrap_rejects_bad(self):
        e, _ = _engine()
        self.assertEqual(e.bootstrap("BTCUSDT", 0), [])
        self.assertEqual(e.bootstrap("BTCUSDT", -5), [])
        self.assertIsNone(e.feeds["BTCUSDT"].anchor)

    def test_return_computed(self):
        e, _ = _engine()
        e.bootstrap("BTCUSDT", 1000.0)
        e.on_mid("BTCUSDT", 1005.0)
        self.assertAlmostEqual(e.ret("BTCUSDT"), 0.005)

    def test_lazy_bootstrap_from_mid(self):
        e, _ = _engine()
        e.on_mid("BTCUSDT", 500.0)                             # no anchor yet -> becomes anchor
        self.assertEqual(e.feeds["BTCUSDT"].anchor, 500.0)
        self.assertEqual(e.ret("BTCUSDT"), 0.0)


class TestArmDisarmHysteresis(unittest.TestCase):
    def test_arms_above_threshold(self):
        e, _ = _engine(arm=0.0045)
        e.bootstrap("BTCUSDT", 1000.0)
        self.assertEqual(e.on_mid("BTCUSDT", 1004.0), [])     # +0.40% < 0.45% -> no arm
        evs = e.on_mid("BTCUSDT", 1005.0)                     # +0.50% >= 0.45% -> arm
        self.assertEqual(len(evs), 1)
        a = evs[0]
        self.assertEqual(a["event"], "arm")
        self.assertEqual(a["feed"], "BTC")
        self.assertEqual(a["dir"], "up")
        self.assertEqual(a["ret_bps"], 50)
        self.assertTrue(e.feeds["BTCUSDT"].armed)

    def test_arms_on_downmove(self):
        e, _ = _engine()
        e.bootstrap("ETHUSDT", 2000.0)
        evs = e.on_mid("ETHUSDT", 1988.0)                     # -0.60%
        self.assertEqual(evs[0]["dir"], "down")
        self.assertLess(evs[0]["ret_pct"], 0)

    def test_hysteresis_holds_then_disarms(self):
        e, clock = _engine(arm=0.0045, disarm=0.0035)
        e.bootstrap("BTCUSDT", 1000.0)
        e.on_mid("BTCUSDT", 1005.0)                           # arm at +0.50%
        clock.t += 5
        self.assertEqual(e.on_mid("BTCUSDT", 1004.0), [])     # +0.40% still in band -> stay armed
        self.assertTrue(e.feeds["BTCUSDT"].armed)
        evs = e.on_mid("BTCUSDT", 1003.0)                     # +0.30% < 0.35% -> disarm (FP)
        self.assertEqual(evs[0]["event"], "disarm")
        self.assertAlmostEqual(evs[0]["held_s"], 5.0)
        self.assertFalse(e.feeds["BTCUSDT"].armed)
        # peak return tracked the +0.50% high, not the disarm level
        self.assertAlmostEqual(evs[0]["peak_ret_pct"], 0.5, places=6)

    def test_no_double_arm_while_armed(self):
        e, _ = _engine()
        e.bootstrap("BTCUSDT", 1000.0)
        self.assertEqual(len(e.on_mid("BTCUSDT", 1006.0)), 1)  # arm
        self.assertEqual(e.on_mid("BTCUSDT", 1007.0), [])      # already armed -> no new event


class TestConfirmedAndAnchorReset(unittest.TestCase):
    def test_confirmed_measures_lead_and_resets_anchor(self):
        e, clock = _engine()
        e.bootstrap("BTCUSDT", 1000.0)
        e.on_mid("BTCUSDT", 1006.0)                           # arm at t=1000
        clock.t += 33.5                                       # push arrives 33.5s later
        evs = e.on_push("BTCUSDT", wall=7.0, price=1006.0)
        self.assertEqual(evs[0]["event"], "confirmed")
        self.assertTrue(evs[0]["was_armed"])
        self.assertAlmostEqual(evs[0]["lead_s"], 33.5)
        # anchor reset to the CURRENT binance mid (~ the freshly pushed value) -> return ~0
        self.assertEqual(e.feeds["BTCUSDT"].anchor, 1006.0)
        self.assertAlmostEqual(e.ret("BTCUSDT"), 0.0)
        self.assertFalse(e.feeds["BTCUSDT"].armed)

    def test_unarmed_push_is_recall_miss(self):
        e, _ = _engine()
        e.bootstrap("BTCUSDT", 1000.0)
        e.on_mid("BTCUSDT", 1002.0)                           # +0.20% -> never armed
        evs = e.on_push("BTCUSDT", price=1002.0)
        self.assertEqual(evs[0]["event"], "push")
        self.assertFalse(evs[0]["was_armed"])
        self.assertIsNone(evs[0]["lead_s"])

    def test_push_reanchors_to_price_when_no_mid(self):
        e, _ = _engine()
        # no mid ever seen -> anchor falls back to the reported on-chain price
        evs = e.on_push("BTCUSDT", price=1500.0)
        self.assertEqual(evs[0]["event"], "push")
        self.assertEqual(e.feeds["BTCUSDT"].anchor, 1500.0)


class TestFalsePositiveWindow(unittest.TestCase):
    def test_stuck_arm_times_out_and_suppresses(self):
        e, clock = _engine(arm=0.0045, disarm=0.0035, window=90.0)
        e.bootstrap("BTCUSDT", 1000.0)
        e.on_mid("BTCUSDT", 1006.0)                           # arm
        self.assertEqual(e.tick(), [])                        # not yet past window
        clock.t += 91
        e.on_mid("BTCUSDT", 1006.0)                           # still elevated (no retrace)
        evs = e.tick()
        self.assertEqual(evs[0]["event"], "falsepos")
        self.assertGreaterEqual(evs[0]["held_s"], 90.0)
        self.assertFalse(e.feeds["BTCUSDT"].armed)
        # suppressed: still-elevated mid must NOT immediately re-arm
        self.assertEqual(e.on_mid("BTCUSDT", 1006.0), [])
        # a retrace below the band clears suppression, then a fresh cross re-arms
        self.assertEqual(e.on_mid("BTCUSDT", 1002.0), [])     # clears suppression, no event
        self.assertEqual(len(e.on_mid("BTCUSDT", 1006.0)), 1)  # re-arms


class TestFormatGrammar(unittest.TestCase):
    def test_line_prefix_and_dashes(self):
        line = pr.format_line({"event": "confirmed", "feed": "BTC", "lead_s": None,
                               "was_armed": True, "ret_pct": 0.5, "ts": 123.0})
        self.assertTrue(line.startswith("PREDICT event=confirmed feed=BTC "))
        self.assertIn("lead_s=-", line)                       # None -> '-'
        self.assertIn("was_armed=1", line)                    # bool -> 1
        self.assertIn("ret_pct=0.5", line)                    # no false trailing zeros
        self.assertIn("ts=123", line)

    def test_markets_for_symbol(self):
        self.assertEqual(pr.markets_for_symbol("BTCUSDT"), oc.FEED_MARKETS["BTC/USD"])
        self.assertTrue(pr.markets_for_symbol("BTCUSDT"))     # non-empty
        self.assertEqual(pr.markets_for_symbol("NOPE"), set())


# --- driver ---------------------------------------------------------------------------------
class _DriverClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def wall(self):
        return 100.0 + self.t


class TestDriver(unittest.TestCase):
    def _driver(self, mids, polls, on_arm=None, clock=None):
        """mids: dict symbol->value (static); polls: list of poll_fn return dicts, consumed
        one per poll cadence hit."""
        clock = clock or _DriverClock()
        eng = pr.PredictEngine(("BTCUSDT", "ETHUSDT"), arm_pct=0.0045, disarm_pct=0.0035,
                               now=clock.now)
        self.logs = []
        pq = list(polls)

        def poll_fn():
            return pq.pop(0) if pq else {}
        d = pr.PredictDriver(eng, mid_fn=lambda s: mids.get(s), poll_fn=poll_fn, on_arm=on_arm,
                             log=self.logs.append, interval=0.5, poll_interval=2.0,
                             now=clock.now, wall=clock.wall)
        return d, eng, clock

    def test_bootstrap_then_push_detected(self):
        d, eng, clock = self._driver(
            mids={"BTCUSDT": 1000.0, "ETHUSDT": 2000.0},
            polls=[{"BTCUSDT": (500, 1000.0), "ETHUSDT": (600, 2000.0)},
                   {"BTCUSDT": (500, 1000.0), "ETHUSDT": (600, 2000.0)},   # same updatedAt
                   {"BTCUSDT": (777, 1000.0), "ETHUSDT": (600, 2000.0)}])  # BTC pushed
        d.step()                                              # poll #1 bootstraps anchors
        self.assertEqual(eng.feeds["BTCUSDT"].anchor, 1000.0)
        clock.t += 2.0
        d.step()                                              # poll #2, no updatedAt change
        clock.t += 2.0
        # arm BTC first so the push registers as 'confirmed'
        eng.on_mid("BTCUSDT", 1006.0)
        d.step()                                              # poll #3 sees BTC updatedAt change
        kinds = [l.split("event=")[1].split(" ")[0] for l in self.logs if l.startswith("PREDICT")]
        self.assertIn("confirmed", kinds)

    def test_publish_only_on_change(self):
        calls = []
        d, eng, clock = self._driver(
            mids={"BTCUSDT": 1006.0, "ETHUSDT": 2000.0},
            polls=[{"BTCUSDT": (1, 1000.0), "ETHUSDT": (1, 2000.0)}],
            on_arm=calls.append)
        d.step()                                              # bootstrap + BTC arms (+0.6%)
        self.assertEqual(calls[-1], {"BTCUSDT"})              # published armed set
        n = len(calls)
        d.step()                                              # nothing changed -> no republish
        self.assertEqual(len(calls), n)

    def test_poll_failure_isolated(self):
        clock = _DriverClock()
        eng = pr.PredictEngine(("BTCUSDT",), now=clock.now)

        def boom():
            raise RuntimeError("rpc down")
        logs = []
        d = pr.PredictDriver(eng, mid_fn=lambda s: 1000.0, poll_fn=boom, log=logs.append,
                             now=clock.now, wall=lambda: 0.0)
        d.step()                                              # must not raise
        self.assertEqual(d.stats["poll_fail"], 1)
        self.assertEqual(d.stats["steps"], 1)

    def test_prediction_without_push_never_confirms(self):
        """The core safety property: arming on a prediction, with NO push, yields an 'arm' (and a
        pre-arm publish) but NEVER a 'confirmed' — the driver has no send path at all."""
        published = []
        d, eng, clock = self._driver(
            mids={"BTCUSDT": 1006.0, "ETHUSDT": 2000.0},
            polls=[{"BTCUSDT": (1, 1000.0), "ETHUSDT": (1, 2000.0)}],   # bootstrap, no later push
            on_arm=published.append)
        for _ in range(5):
            d.step()
            clock.t += 0.5
        kinds = [l.split("event=")[1].split(" ")[0] for l in self.logs if l.startswith("PREDICT")]
        self.assertIn("arm", kinds)
        self.assertNotIn("confirmed", kinds)                 # no push -> never confirmed
        self.assertNotIn("push", kinds)
        self.assertEqual(published[-1], {"BTCUSDT"})         # pre-armed (prepared), not fired
        self.assertEqual(d.stats["confirmed"], 0)
        # the driver exposes no broadcast/sign surface
        self.assertFalse(hasattr(d, "_rpc_write"))
        self.assertFalse(hasattr(d, "send"))


if __name__ == "__main__":
    unittest.main()
