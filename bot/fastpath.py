"""Predictive block-boundary detection + flip-threshold math for the pre-armed fire path.

MEASURED (probe harness ~/.katana-probe, 551 probes, 2026-07-16/17): Katana blocks tick at
~1.000s and the sequencer's effective cutoff for NEXT-block inclusion is ~0.25-0.35s after
block N first becomes visible via the public RPC — P(next block) ≈ 21% at submit-offset
+0.05s, 9-13% at +0.15-0.25s, ~0% at ≥+0.35s; send one-way ≈ 110-150ms. The classic detect
(fixed-cadence full scan, 0.65-1.05s) alone forfeited that window every time.

This module phase-locks the block tick and splits each period for the executor:
  * idle zone [t0, t0+WINDOW):  ALL maintenance RPC/quote traffic happens here (hot pass,
    Sushi re-quotes, pre-sign, warm-up pings) — the armed window opens quiet;
  * armed zone [t0+WINDOW, ..): eth_blockNumber tight-poll every ~STEP on the keep-alive
    read lane until N+1 appears — detect latency ≈ STEP/2 + RTT instead of ~1s.
On detect the executor sends ONE pre-built aggregate3 multicall for the hot-set oracle
prices and compares them to per-target flip thresholds pre-computed here with the exact
integer mirror of Morpho.sol _isHealthy — a flipped pre-armed target then fires with zero
further lookups (bot/executor.py fast path).

Falls back loudly (None returns) whenever the pattern breaks — RPC hiccup, slow pass
overrun, multi-second block gap — so the executor drops to the classic cadence instead of
trusting a stale phase. Pure math + injected-clock timing only: no signing, no state, no
imports from bot.executor; unit-tested offline in test_fastpath.py.
"""
from __future__ import annotations

import os
import time

from analysis.keccak import selector
from analysis.models import ORACLE_PRICE_SCALE, WAD
from analysis.multicall import decode_aggregate3, encode_aggregate3

SEL_PRICE = selector("price()")

BLOCK_SEC = float(os.environ.get("KT_BLOCK_SEC", "1.0"))
# idle/armed split: sleep until t0+WINDOW, then tight-poll every STEP; give up (pattern
# broke) SLACK after the expected boundary. All measured against the ~1.000s tick.
WINDOW_SEC = float(os.environ.get("KT_PREDICT_WINDOW", "0.80"))
STEP_SEC = float(os.environ.get("KT_PREDICT_STEP", "0.018"))
SLACK_SEC = float(os.environ.get("KT_PREDICT_SLACK", "0.75"))

# sentinel flip threshold: liquidatable at ANY oracle price (no collateral backing the debt)
ALWAYS_FLIP = 1 << 256


# --- flip thresholds (exact integer Morpho _isHealthy mirror) --------------------------
def min_healthy_price(collateral: int, lltv: int, debt_assets: int) -> int:
    """Smallest oracle price at which the position is HEALTHY — i.e. the flip threshold:
    HF < 1  ⟺  price < min_healthy_price(...). Exact integer mirror of Morpho.sol:
        maxBorrow = wMulDown(mulDivDown(collateral, price, 1e36), lltv);  healthy ⟺ maxBorrow >= debt
    Derivation (all floor division, so the bound is exact, never off by one):
        floor(floor(coll*P/S)*lltv/W) >= d  ⟺  floor(coll*P/S) >= ceil(d*W/lltv)  =: m
                                            ⟺  P >= ceil(m*S/coll)
    Returns 0 when there is no debt (never flips) and ALWAYS_FLIP when debt is backed by no
    collateral/lltv (liquidatable at any price)."""
    if debt_assets <= 0:
        return 0
    if collateral <= 0 or lltv <= 0:
        return ALWAYS_FLIP
    m = (debt_assets * WAD + lltv - 1) // lltv
    return (m * ORACLE_PRICE_SCALE + collateral - 1) // collateral


def attach_flip_thresholds(rows: list[dict]) -> list[dict]:
    """Watch rows for the armed window: every not-yet-liquidatable hot row (HF >= 1) with its
    pre-computed flip threshold under 'flip_px'. Rows already below HF 1 are the classic
    pass's job — the flip concept does not apply to them."""
    return [dict(r, flip_px=min_healthy_price(r["collateral"], r["lltv"], r["debt_assets"]))
            for r in rows if r["hf"] >= 1.0]


def build_price_refresh(rows: list[dict]) -> tuple[str, list[str]]:
    """ONE pre-built aggregate3 calldata reading price() for every distinct oracle in the
    hot set, plus the oracle order for decoding. Built during the idle zone so the armed
    window spends zero time encoding."""
    oracles: list[str] = []
    for r in rows:
        if r["oracle"] not in oracles:
            oracles.append(r["oracle"])
    return encode_aggregate3([(o, SEL_PRICE) for o in oracles]), oracles


def decode_price_refresh(ret_hex: str, oracles: list[str]) -> dict[str, int]:
    """{oracle: price} from the aggregate3 return; failed/short reads are simply absent
    (their markets just don't participate in this window's flip check)."""
    out: dict[str, int] = {}
    for o, (ok, ret) in zip(oracles, decode_aggregate3(ret_hex)):
        if ok and len(ret) >= 66:
            out[o] = int(ret[2:66], 16)
    return out


def flipped(rows: list[dict], prices: dict[str, int]) -> list[dict]:
    """Rows whose market crossed liquidatable at the fresh prices (price < flip_px),
    biggest debt first — the ordering the fire path wants."""
    hits = [r for r in rows
            if prices.get(r["oracle"]) is not None and prices[r["oracle"]] < r["flip_px"]]
    return sorted(hits, key=lambda r: -(r.get("debt_usd") or 0))


# --- block phase lock -------------------------------------------------------------------
class BlockClock:
    """Phase lock on the ~1.000s block cadence. sync() coarse-polls until a block transition
    is OBSERVED (that instant anchors the phase, error <= coarse step + RTT); wait_next()
    sleeps out the idle zone then tight-polls the armed zone (re-anchoring on its own detect,
    error <= STEP + RTT, so there is no cumulative drift). Both return None whenever the
    boundary time cannot be trusted — first armed poll already new (anchor would be late by
    up to WINDOW), a skipped block, an overrun pass, or a dead RPC — and invalidate the
    phase so the caller re-syncs or falls back to the classic cadence.

    `poll` is injected: a zero-retry keep-alive eth_blockNumber returning int | None
    (analysis.rpc.Rpc.poll_block_number). `now`/`sleep` are injectable for offline tests."""

    def __init__(self, poll, block_sec: float = BLOCK_SEC, window: float = WINDOW_SEC,
                 step: float = STEP_SEC, slack: float = SLACK_SEC,
                 now=time.monotonic, sleep=time.sleep):
        self.poll = poll
        self.block_sec, self.window, self.step, self.slack = block_sec, window, step, slack
        self._now, self._sleep = now, sleep
        self.block: int | None = None   # last block seen
        self.t0: float | None = None    # monotonic instant its transition was DETECTED

    @property
    def synced(self) -> bool:
        return self.t0 is not None and self.block is not None

    def idle_remaining(self) -> float:
        """Seconds left in the idle zone — the budget for quote/pre-sign maintenance so the
        armed window opens free of any other RPC traffic. 0 when the phase is unknown."""
        if self.t0 is None:
            return 0.0
        return max(0.0, self.t0 + self.window - self._now())

    def sync(self, coarse: float | None = None) -> tuple[int, float] | None:
        """Lock the phase: poll at a coarse cadence until a block transition is observed
        (<= ~1 block of polling; anchor error <= coarse + RTT — the next wait_next() detect
        tightens it to <= STEP). None if no tick shows within ~2.5 blocks (RPC down/stalled)."""
        coarse = self.step * 3 if coarse is None else coarse
        base = None
        deadline = self._now() + 2.5 * self.block_sec
        while self._now() < deadline:
            bn = self.poll()
            t = self._now()
            if bn is not None:
                if base is None:
                    base = bn
                elif bn > base:
                    self.block, self.t0 = bn, t
                    return bn, t
            self._sleep(coarse)
        self.t0 = None
        return None

    def wait_next(self) -> tuple[int, float] | None:
        """Block-boundary wait: sleep until t0+WINDOW, then tight-poll every STEP until the
        next block appears. Returns (block, t_detect) — t_detect ≈ first RPC visibility —
        and re-anchors the phase on it. None (phase invalidated) when the pattern broke."""
        if self.block is None or self.t0 is None:
            return None
        target = self.t0 + self.window
        deadline = self.t0 + self.block_sec + self.slack
        now = self._now()
        if now > deadline:               # slow pass overran the whole period — phase stale
            self.t0 = None
            return None
        if now < target:
            self._sleep(target - now)
        saw_old = False
        while True:
            bn = self.poll()
            t = self._now()
            if bn is not None and bn > self.block:
                jumped = bn - self.block
                self.block = bn
                if saw_old and jumped == 1:
                    self.t0 = t          # boundary within (prev poll, now] — tight anchor
                    return bn, t
                # first armed poll was already new, or we skipped a block: the boundary
                # instant is unknown — anchoring now would be late by up to WINDOW and the
                # error would compound. Re-sync instead.
                self.t0 = None
                return None
            if bn is not None:
                saw_old = True
            if t >= deadline:
                self.t0 = None           # no tick within slack — RPC hiccup / cadence break
                return None
            self._sleep(max(0.0, self.step - (self._now() - t)))
