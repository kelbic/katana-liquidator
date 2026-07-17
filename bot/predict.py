"""Oracle-push PREDICTION pre-arm layer: anchor/return/hysteresis state machine + driver (behind
KT_PREDICT, default-SHADOW; importing this module opens nothing, no threads, no network).

PRINCIPLE (do not violate): prediction is a PREPARATION edge, NOT an overtake. We CANNOT fire a
liquidation before the on-chain push — the position isn't liquidatable until the oracle reprices
on-chain, and the exact per-round tip must still be read from the pending oracle tx in the
mempool. So this layer NEVER fires anything: it only PRE-ARMS (widens the pre-signed flip-set,
warms the write lane, keeps the block-locked cadence). The actual fire stays exactly as today —
the v4 mempool layer, on the CONFIRMED pending oracle tx, tip-matches and broadcasts. Firing
speculatively on a prediction is FORBIDDEN (the ~46% false-positive rate would revert/waste gas).

MODEL: Chainlink pushes when |binance_mid - last_onchain| / last_onchain >= ~0.5% (or heartbeat).
Per feed we hold an ANCHOR = the on-chain price at the last push, and continuously compute
return = (binance_mid - anchor)/anchor. We ARM at |return| >= KT_PREDICT_ARM_PCT (default 0.45%,
just below the 0.5% trigger, to buy lead) and DISARM on a retrace below KT_PREDICT_DISARM_PCT
(0.35%, a hysteresis band) with no push. An arm PERSISTS while the price stays deviated — it is
released ONLY on a genuine retrace (disarm), never on a timer, because a real push can lag by
minutes (research lead p90 = 132s BTC / 325s ETH). On a confirmed push we reset the anchor to the
current Binance mid (which ≈ the freshly-pushed value → return ≈ 0). An arm that never sees a push
is a false positive (retrace => `disarm`; held deviated past the generous KT_PREDICT_HOLD_SEC cap
with no push => `falsepos`).

  PredictEngine  — pure state machine (this module's core; unit-tested offline, no I/O).
  PredictDriver  — daemon-thread orchestrator: reads the PriceFeed mid snapshot + polls the
                   aggregators' latestRoundData (injected), drives the engine, formats+logs the
                   PREDICT lines, and (LIVE only, via on_arm/on_disarm) publishes the armed
                   market set the executor's _arm_candidates widens on. Injectable clock/poll so
                   the loop body is tested with zero network/threads (test_predict.py).

PREDICT shadow-log grammar (greppable; `PREDICT ` + space-separated key=value, `-` = no value):
  event=bootstrap  feed anchor source            — startup anchor set from the on-chain price.
  event=arm        feed ret_pct ret_bps anchor mid dir           — a feed just crossed ARM_PCT.
  event=confirmed  feed was_armed=1 lead_s ret_pct arm_ret_pct anchor push_mid
                                                 — a push arrived while ARMED. lead_s = push_ts −
                                                   arm_ts is the measured readiness head start.
  event=push       feed was_armed=0 ret_pct anchor push_mid      — a push with NO active arm (a
                                                   recall miss / sub-threshold move). lead_s = -.
  event=disarm     feed held_s ret_pct peak_ret_pct              — armed → retraced below the
                                                   hysteresis band, no push (false positive).
  event=falsepos   feed held_s ret_pct peak_ret_pct              — armed, held DEVIATED past the
                                                   KT_PREDICT_HOLD_SEC cap (600s) with no push and
                                                   no retrace (real Binance-vs-median disagreement).
  event=prearm     feed markets n                — (LIVE) published N market(s) for widened arm.
  event=prearm_clear feed                        — (LIVE) cleared a feed's widened-arm markets.
Analyzer notes: FP rate = (disarm + falsepos) / arm; recall = confirmed / (confirmed + push);
lead-time distribution = lead_s over `confirmed`. ret_pct is signed % (dir up/down), ret_bps its
integer basis-points magnitude. Everything is measured on OUR OWN live flow — validating the
research's ~30-40s lead / ~46% FP before live pre-arm is ever switched on.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from bot import oracles

# Binance symbol -> Chainlink oracle feed name (bot/oracles.FEEDS) -> its markets. Only the two
# deviation-driven feeds the pricefeed tracks; extend both maps together to add a feed.
SYMBOL_FEED: dict[str, str] = {"BTCUSDT": "BTC/USD", "ETHUSDT": "ETH/USD"}
FEED_LABEL: dict[str, str] = {"BTC/USD": "BTC", "ETH/USD": "ETH"}

ARM_PCT = 0.0045
DISARM_PCT = 0.0035
# Hold cap: an arm PERSISTS while the price stays deviated (>= DISARM_PCT) and is released only on
# a genuine retrace (disarm) — NOT on a timer. This cap is the last-resort release when the price
# stays deviated but no push ever comes (a real Binance-vs-Chainlink-median disagreement). It must
# comfortably exceed the measured push lead (research p90 = 132s BTC / 325s ETH; a 90s cap wrongly
# cleared genuine slow-build true positives and mislabelled them falsepos), hence 600s default.
HOLD_SEC = 600.0


def markets_for_symbol(symbol: str) -> set[str]:
    """Lower-case marketIds whose Morpho oracle price depends on this Binance symbol's feed."""
    return set(oracles.FEED_MARKETS.get(SYMBOL_FEED.get(symbol.upper(), ""), set()))


# --- log formatting (pure; mirrors executor._mempool_log's fixed-point, no-false-0.0 rules) ---
def format_line(event: dict) -> str:
    def fmt(v):
        if v is None:
            return "-"
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, float):
            r = round(v, 6)
            return str(int(r)) if r == int(r) else format(r, "f").rstrip("0").rstrip(".")
        return str(v)
    ev = dict(event)
    ev.setdefault("ts", round(time.time(), 3))
    return "PREDICT " + " ".join(f"{k}={fmt(v)}" for k, v in ev.items())


# --- pure state machine ---------------------------------------------------------------------
@dataclass
class Feed:
    symbol: str
    feed: str
    label: str
    anchor: float | None = None
    mid: float | None = None
    armed: bool = False
    suppressed: bool = False          # after a falsepos timeout: no re-arm until a retrace clears
    arm_mono: float | None = None
    arm_wall: float | None = None
    arm_return: float | None = None
    peak_return: float = 0.0          # signed return of largest magnitude seen since this arm


class PredictEngine:
    """Anchor/return/hysteresis per feed. All inputs are pushed in (on_mid/on_push/tick); every
    method returns a list of event dicts (never logs, never does I/O). `now` is injectable."""

    def __init__(self, symbols=("BTCUSDT", "ETHUSDT"), arm_pct: float = ARM_PCT,
                 disarm_pct: float = DISARM_PCT, falsepos_window: float = HOLD_SEC,
                 now=time.monotonic):
        self.arm_pct, self.disarm_pct, self.falsepos_window = arm_pct, disarm_pct, falsepos_window
        self._now = now
        self.feeds: dict[str, Feed] = {}
        for s in symbols:
            s = s.upper()
            if s in SYMBOL_FEED:
                self.feeds[s] = Feed(s, SYMBOL_FEED[s], FEED_LABEL[SYMBOL_FEED[s]])

    def ret(self, symbol: str) -> float | None:
        f = self.feeds.get(symbol.upper())
        if f is None or f.anchor is None or f.mid is None or f.anchor == 0:
            return None
        return (f.mid - f.anchor) / f.anchor

    def armed_symbols(self) -> set[str]:
        return {s for s, f in self.feeds.items() if f.armed}

    def bootstrap(self, symbol: str, anchor: float, source: str = "onchain") -> list[dict]:
        """Seed a feed's anchor from the current on-chain price. Idempotent — only sets it once
        (the first push then re-anchors to the Binance mid)."""
        f = self.feeds.get(symbol.upper())
        if f is None or f.anchor is not None or anchor is None or anchor <= 0:
            return []
        f.anchor = float(anchor)
        return [{"event": "bootstrap", "feed": f.label, "anchor": f.anchor, "source": source}]

    def on_mid(self, symbol: str, mid: float, wall: float | None = None) -> list[dict]:
        f = self.feeds.get(symbol.upper())
        if f is None or mid is None or mid <= 0:
            return []
        f.mid = float(mid)
        if f.anchor is None:                       # lazy bootstrap if the on-chain read was late
            f.anchor = f.mid
            return []
        r = self.ret(symbol)
        if r is None:
            return []
        mag = abs(r)
        events: list[dict] = []
        if f.armed and mag > abs(f.peak_return):
            f.peak_return = r
        if not f.armed:
            if f.suppressed and mag < self.disarm_pct:
                f.suppressed = False               # retrace cleared the post-falsepos suppression
            if not f.suppressed and mag >= self.arm_pct:
                f.armed = True
                f.arm_mono = self._now()
                f.arm_wall = wall if wall is not None else time.time()
                f.arm_return = r
                f.peak_return = r
                events.append({"event": "arm", "feed": f.label, "ret_pct": r * 100.0,
                               "ret_bps": int(round(mag * 10000.0)), "anchor": f.anchor,
                               "mid": f.mid, "dir": ("up" if r >= 0 else "down")})
        elif mag < self.disarm_pct:                # retrace below the hysteresis band, no push
            events.append(self._end(f, "disarm", r))
        return events

    def on_push(self, symbol: str, wall: float | None = None,
                price: float | None = None) -> list[dict]:
        """A CONFIRMED on-chain push for this feed (aggregator latestRoundData updatedAt moved).
        Emits confirmed (if armed) or push (recall miss), then re-anchors to the current Binance
        mid so the return resets to ~0."""
        f = self.feeds.get(symbol.upper())
        if f is None:
            return []
        r = self.ret(symbol)
        events: list[dict] = []
        if f.armed:
            lead = (self._now() - f.arm_mono) if f.arm_mono is not None else None
            events.append({"event": "confirmed", "feed": f.label, "was_armed": True,
                           "lead_s": (None if lead is None else round(lead, 3)),
                           "ret_pct": (None if r is None else r * 100.0),
                           "arm_ret_pct": (None if f.arm_return is None else f.arm_return * 100.0),
                           "anchor": f.anchor, "push_mid": f.mid})
        else:
            events.append({"event": "push", "feed": f.label, "was_armed": False, "lead_s": None,
                           "ret_pct": (None if r is None else r * 100.0),
                           "anchor": f.anchor, "push_mid": f.mid})
        # re-anchor to the current Binance mid (≈ the freshly-pushed on-chain value); fall back to
        # the reported on-chain price only if we have no mid yet.
        new_anchor = f.mid if f.mid is not None else price
        if new_anchor is not None and new_anchor > 0:
            f.anchor = float(new_anchor)
        f.armed = False
        f.suppressed = False
        f.arm_mono = f.arm_wall = f.arm_return = None
        f.peak_return = 0.0
        return events

    def tick(self, wall: float | None = None) -> list[dict]:
        """Time-driven LAST-RESORT release. An arm PERSISTS while the price stays deviated — the
        normal release is disarm-on-retrace (on_mid), never a timer, because a genuine push can
        lag by minutes (research lead p90 = 132s BTC / 325s ETH). Only when the price has held
        deviated past the generous HOLD_SEC cap with no push do we give up: a real Binance-vs-
        Chainlink-median disagreement -> falsepos. Disarm + suppress re-arm until a retrace clears
        it (so we don't arm→falsepos→arm loop while the price sits just above the band)."""
        events: list[dict] = []
        for f in self.feeds.values():
            if f.armed and f.arm_mono is not None and \
                    (self._now() - f.arm_mono) >= self.falsepos_window:
                r = self.ret(f.symbol)
                events.append(self._end(f, "falsepos", r))
                f.suppressed = True
        return events

    def _end(self, f: Feed, kind: str, r: float | None) -> dict:
        held = (self._now() - f.arm_mono) if f.arm_mono is not None else None
        peak = f.peak_return
        ev = {"event": kind, "feed": f.label,
              "held_s": (None if held is None else round(held, 3)),
              "ret_pct": (None if r is None else r * 100.0),
              "peak_ret_pct": (None if peak == 0 else peak * 100.0)}
        f.armed = False
        f.arm_mono = f.arm_wall = f.arm_return = None
        f.peak_return = 0.0
        return ev


# --- daemon-thread orchestrator -------------------------------------------------------------
class PredictDriver:
    """Background thread wiring the PriceFeed + aggregator poll into the engine. It NEVER signs or
    sends — it only drives the engine, logs, and (LIVE) publishes armed markets via on_arm/
    on_disarm. Isolated exactly like MempoolClient: own thread, own injected read lane; every
    step is wrapped so a poll/parse error degrades to 'no prediction this step', never a wedge.

    mid_fn(symbol)->float|None            latest Binance mid (PriceFeed.mid).
    poll_fn()-> {symbol: (updatedAt:int, price:float)} | {}   aggregator latestRoundData read.
    on_arm(armed_symbols:set[str])        called ONLY when the armed set changes (arm OR disarm)
                                          with the CURRENT set of armed symbols. LIVE publishes
                                          the widened markets; SHADOW passes None so there is zero
                                          fire-state effect (measure only).
    """

    def __init__(self, engine: PredictEngine, mid_fn, poll_fn, on_arm=None,
                 log=print, interval: float = 0.5, poll_interval: float = 2.0,
                 now=time.monotonic, sleep=time.sleep, wall=time.time):
        self.engine = engine
        self.mid_fn, self.poll_fn = mid_fn, poll_fn
        self.on_arm = on_arm
        self.log = log
        self.interval, self.poll_interval = interval, poll_interval
        self._now, self._sleep, self._wall = now, sleep, wall
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_updated: dict[str, int] = {}
        self._last_poll: float | None = None       # None -> the first step polls immediately
        self._prev_armed: set[str] = set()
        self.stats = {"steps": 0, "arms": 0, "confirmed": 0, "falsepos": 0, "poll_fail": 0}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="predict-driver", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:                  # a driver bug must never kill the thread
                self.log(f"[predict] driver step error: {str(e)[:160]}")
            self._sleep(self.interval)

    def step(self) -> None:
        """One drive tick (public for offline tests): poll aggregators on cadence -> feed pushes
        + lazy bootstrap, feed the latest mids, run the falsepos timer, log all events, and
        publish the armed-market delta."""
        self.stats["steps"] += 1
        wall = self._wall()
        events: list[dict] = []
        if self._last_poll is None or (self._now() - self._last_poll) >= self.poll_interval:
            self._last_poll = self._now()
            events += self._poll(wall)
        for sym in self.engine.feeds:
            mid = self.mid_fn(sym)
            if mid is not None:
                events += self.engine.on_mid(sym, mid, wall)
        events += self.engine.tick(wall)
        for ev in events:
            self.log(format_line(ev))
            if ev["event"] == "arm":
                self.stats["arms"] += 1
            elif ev["event"] == "confirmed":
                self.stats["confirmed"] += 1
            elif ev["event"] == "falsepos":
                self.stats["falsepos"] += 1
        self._publish()

    def _poll(self, wall: float) -> list[dict]:
        try:
            reads = self.poll_fn() or {}
        except Exception as e:
            self.stats["poll_fail"] += 1
            self.log(f"[predict] aggregator poll failed (skipped): {str(e)[:120]}")
            return []
        events: list[dict] = []
        for sym, val in reads.items():
            sym = sym.upper()
            if sym not in self.engine.feeds or not val:
                continue
            updated_at, price = val
            f = self.engine.feeds[sym]
            if f.anchor is None:                        # first read -> bootstrap the anchor
                events += self.engine.bootstrap(sym, price)
                self._last_updated[sym] = updated_at
            elif sym not in self._last_updated:
                self._last_updated[sym] = updated_at    # first poll baseline (anchor was lazy-set
                #                                         from a mid before any poll) — NOT a push
            elif updated_at != self._last_updated[sym]:
                events += self.engine.on_push(sym, wall, price)
                self._last_updated[sym] = updated_at
        return events

    def _publish(self) -> None:
        armed = self.engine.armed_symbols()
        if armed == self._prev_armed:
            return
        self._prev_armed = set(armed)
        if self.on_arm is not None:
            try:
                self.on_arm(armed)
            except Exception as e:
                self.log(f"[predict] on_arm error: {str(e)[:120]}")
