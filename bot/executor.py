"""Katana Morpho live-signing liquidation executor.

Autonomous loop: monitor.scan() finds HF<1 positions -> evaluate() sizes the exit against a
LIVE Sushi quote (chunking under depth so slippage never eats the LIF bonus) -> fire() signs
and broadcasts an atomic KatanaLiquidator.liquidate() tx. Ported from the production Midnight
(Base) live executor; same capital-protection discipline.

CAPITAL PROTECTION (multiple independent layers):
  * DRY_RUN=1 by default — logs what it WOULD do, never sends.
  * Off-chain net gate: fires only if quoted net (proceeds - repaid - gas) >= KT_MIN_PROFIT_USD.
  * On-chain minProfit gate (2nd layer): the tx reverts unless realised surplus >= floor. Even a
    stale/optimistic quote cannot execute a losing liquidation.
  * Swap-input haircut (bot/sushi.py): the RouteProcessor amountIn is quoted slightly below the
    real seized amount, so an adverse tick can't make the swap over-pull and revert mid-tx.
  * KILL-SWITCH: daily gas cap + consecutive-revert cap -> stop + alert.
  * DEDUP: (market,borrower) journal — don't re-fire a recent target.
  * Chunking: large/thin exits are split into the largest chunk whose net clears the floor;
    the rest is taken on later passes as the position stays underwater.

Discovery: the position book comes from the Morpho indexer (analysis.morpho_api) by default —
current near-edge borrowers, instantly, NO getLogs scan from block 0 (that is impractical on a
37M-block chain via the public RPC). Exact trigger HF is still computed on-chain via multicall.

Config (env, KT_ prefix): KT_CONTRACT (req for live), KT_PRIVATE_KEY or KT_KEYFILE
  (~/.katana-bot/key), KT_RPC (write, def rpc.katana.network), KT_READ_RPC (read override, e.g.
  a local anvil fork), KT_MIN_PROFIT_USD (20), KT_MIN_DEBT_USD (500), KT_MAX_SLIPPAGE (0.008),
  KT_MAX_IMPACT (0.02), KT_POLL_SEC (20), KT_MAX_DAILY_GAS_USD (5), KT_MAX_CONSEC_REVERTS (3),
  KT_DEDUP_SEC (300), KT_GAS_LIMIT (1_800_000), KT_CHAIN_ID (747474), KT_PRIORITY_GWEI (0.001),
  KT_DISCOVERY (api [default] | logs), KT_API_HF_CEILING (1.15 — discovery watch ceiling),
  KT_CHECKPOINT_BLOCK (only for KT_DISCOVERY=logs; the api path needs no checkpoint),
  KT_HEARTBEAT_SEC (86400), KT_RAW_TX (1=in-process eth_account [default], 0=cast fallback),
  DRY_RUN (1), KT_CHAT_ID + KT_TG_TOKEN (or the telegram channel env file) for alerts,
  KT_QUOTE_TIMEOUT (5) / KT_QUOTE_RETRIES (2) / KT_EVAL_DEADLINE_SEC (10) — fire-path quote
  budget, KT_RECEIPT_WAIT_SEC (20), KT_DEDUP_OK_SEC (10) — post-success re-take delay,
  KT_WRITE_RPCS (comma-separated fallback write endpoints, rotated on transport failure),
  KT_TRANSIENT_BACKOFF_SEC (2) — per-target retry backoff on write-RPC transport errors,
  KT_SEND_ERR_COOLDOWN_SEC (30) + KT_SEND_ERR_ALERT_SEC (600) / KT_SEND_ERR_ALERT_GLOBAL_SEC
  (60) — send-error target cooldown + alert throttles, KT_BALANCE_CHECK_SEC (600) /
  KT_BALANCE_ALERT_SEC (3600) / KT_BALANCE_FIRES (3) — EOA gas-balance guard,
  KT_PREDICTIVE_POLL (1) — block-phase-locked detect + pre-armed fast fire (bot/fastpath.py),
  KT_ARM_HF (1.002) / KT_ARM_MAX_N (4) / KT_ARM_QUOTE_TTL (2.5s) — pre-arm window,
  KT_BLIND_FIRE (1) — skip preflight in the critical path at the default tip only,
  KT_PREDICT_WINDOW (0.80) / KT_PREDICT_STEP (0.018) / KT_PREDICT_SLACK (0.75) /
  KT_BLOCK_SEC (1.0) — boundary timing (bot/fastpath.py).

Go-live preflight: `pip install -r requirements.txt` (eth_account for KT_RAW_TX=1) — the loop
verifies deps/contract/chainId at startup and exits loudly if broken (never silently).

Usage:
    DRY_RUN=1 python3 -m bot.executor once     # single diagnostic pass (safe)
    DRY_RUN=0 KT_CONTRACT=0x.. python3 -m bot.executor loop   # live loop
    python3 -m bot.executor reset              # clear kill-switch / dedup
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from analysis.keccak import selector                                   # noqa: E402
from analysis.models import lif_from_lltv                              # noqa: E402
from analysis.monitor import scan, load_state as load_monitor_state    # noqa: E402
from analysis.multicall import MULTICALL3                              # noqa: E402
from analysis.protocols import MORPHO, STABLES, TOKENS                 # noqa: E402
from analysis.rpc import DEFAULT_RPCS, Rpc                             # noqa: E402
from bot import fastpath                                               # noqa: E402
from bot.sushi import quote, NoRouteError, SWAP_INPUT_HAIRCUT         # noqa: E402

# --- config -------------------------------------------------------------------
CONTRACT = os.environ.get("KT_CONTRACT", "")
PRIVATE_KEY = os.environ.get("KT_PRIVATE_KEY", "")
KEYFILE = os.path.expanduser(os.environ.get("KT_KEYFILE", "~/.katana-bot/key"))
if not PRIVATE_KEY and os.path.exists(KEYFILE):
    PRIVATE_KEY = open(KEYFILE).read().strip()
RPC_WRITE = os.environ.get("KT_RPC", "https://rpc.katana.network")
# Optional fallback write endpoints (comma-separated): on a transport failure _rpc_write rotates
# to the next one instead of erroring out — a single rate-limited/dying write ingress must not
# blind the whole fire path exactly during a cascade.
WRITE_RPCS = [RPC_WRITE] + [u.strip() for u in os.environ.get("KT_WRITE_RPCS", "").split(",")
                            if u.strip() and u.strip() != RPC_WRITE]
READ_RPCS = [os.environ["KT_READ_RPC"]] if os.environ.get("KT_READ_RPC") else None

MIN_PROFIT_USD = float(os.environ.get("KT_MIN_PROFIT_USD", "20"))
MIN_DEBT_USD = float(os.environ.get("KT_MIN_DEBT_USD", "500"))
MAX_SLIPPAGE = float(os.environ.get("KT_MAX_SLIPPAGE", "0.008"))   # swap floor sent to Sushi
MAX_IMPACT = float(os.environ.get("KT_MAX_IMPACT", "0.02"))        # chunk if impact above this
POLL_SEC = int(os.environ.get("KT_POLL_SEC", "20"))         # cadence when NOTHING is near-edge
# Hot-poll: when near-edge positions exist, re-read their HF on-chain this often (cheap; ~8ms RTT
# to the Katana RPC from our region) so we catch a cross within ~HOT_POLL_SEC instead of ~POLL_SEC.
# The Morpho-indexer set is refreshed only every API_REFRESH_SEC to avoid hammering the public API.
HOT_POLL_SEC = float(os.environ.get("KT_HOT_POLL_SEC", "0.3"))  # v2: tight cadence on a small top-N
#   hot set (~block time ~0.8s); effective detection ~0.6s. RPC load ~4-5 req/s, well under the 100/s cap.
API_REFRESH_SEC = float(os.environ.get("KT_API_REFRESH_SEC", "30"))
HOT_HF = float(os.environ.get("KT_HOT_HF", "1.02"))         # hot-poll only when a position is within
#                                                             this HF of liquidation (imminent cross)
GAS_LIMIT = int(os.environ.get("KT_GAS_LIMIT", "1800000"))        # generous (liq+swap+repay+sweep)
GAS_UNITS_EST = int(os.environ.get("KT_GAS_UNITS", "900000"))     # for gas-cost estimate
CHAIN_ID = int(os.environ.get("KT_CHAIN_ID", "747474"))
PRIORITY_GWEI = float(os.environ.get("KT_PRIORITY_GWEI", "0.001"))
# Phase 2 — competitive priority-fee bidding on big CONTESTED tickets. DISABLED by default.
# Katana orders by gas price; the measured single-ticket PGA clears 171-443 gwei, so the default
# 0.001 gwei never wins a contested single. When ON, we bid a fee competitive with the auction but
# capped so we still keep FEE_BID_KEEP_USD of net after paying it. GO-LIVE REQUIRES: a funded wallet
# — the node needs balance >= GAS_LIMIT*maxFee, the FULL envelope: ~0.27-1.08 ETH at 148-600 gwei
# bids (STATE.md funding table; enforced by check_balance) — AND a raised KT_MAX_DAILY_GAS_USD
# (else one bid trips the kill-switch). A lost-but-included bid burns ~125-160k gas at the bid
# price (~$71-90 @300 gwei). Never bids below FEE_BID_MIN_NET_USD tickets.
FEE_BID = os.environ.get("KT_FEE_BID", "0") == "1"
FEE_BID_MIN_NET_USD = float(os.environ.get("KT_FEE_BID_MIN_NET_USD", "300"))
MAX_PRIORITY_GWEI = float(os.environ.get("KT_MAX_PRIORITY_GWEI", "600"))
FEE_BID_KEEP_USD = float(os.environ.get("KT_FEE_BID_KEEP_USD", "50"))
MAX_DAILY_GAS_USD = float(os.environ.get("KT_MAX_DAILY_GAS_USD", "5"))
MAX_CONSEC_REVERTS = int(os.environ.get("KT_MAX_CONSEC_REVERTS", "3"))
DEDUP_SEC = int(os.environ.get("KT_DEDUP_SEC", "300"))
# Don't re-evaluate a target that was just declined ("no profitable chunk") — the perpetual
# bad-debt dregs (HF≈0.6-0.9, no profitable exit) sit near-edge forever, and at hot-poll cadence
# re-quoting them every pass would hammer the Sushi API (evaluate tries up to 8 chunk quotes each).
# A short TTL re-checks them occasionally without spamming; a freshly-crossed target is never in
# this cache so it's still evaluated instantly.
DECLINE_TTL = float(os.environ.get("KT_DECLINE_TTL", "60"))
# A write-RPC transport/rate-limit failure says NOTHING about the target — it must NOT decline
# for DECLINE_TTL (a rate-limited endpoint in a cascade would self-ban every profitable target
# for 60s). Instead: retry next tick, with this short per-target backoff so the hot loop doesn't
# hammer a dying endpoint.
TRANSIENT_BACKOFF_SEC = float(os.environ.get("KT_TRANSIENT_BACKOFF_SEC", "2"))
# Send errors (insufficient funds / bad nonce / RPC down mid-send) are NOT reverts, but without
# a cooldown an unfunded wallet re-ran the FULL evaluate (up to 8 Sushi quotes) + re-fired + TG
# alert for the same target EVERY hot tick (~1s). Cooldown via the sent journal; alerts throttled
# per-target AND globally.
SEND_ERR_COOLDOWN_SEC = float(os.environ.get("KT_SEND_ERR_COOLDOWN_SEC", "30"))
SEND_ERR_ALERT_SEC = float(os.environ.get("KT_SEND_ERR_ALERT_SEC", "600"))        # per target
SEND_ERR_ALERT_GLOBAL_SEC = float(os.environ.get("KT_SEND_ERR_ALERT_GLOBAL_SEC", "60"))
# EOA gas-balance guard: the node REJECTS a tx outright unless balance >= GAS_LIMIT*maxFeePerGas
# — the FULL fee envelope, which with KT_FEE_BID is GAS_LIMIT*(2*base + KT_MAX_PRIORITY_GWEI)
# ≈ 1.08 ETH at the 600 gwei cap, NOT the ~$0.005 a default-tip fire burns (see STATE.md table).
# Checked at startup + every BALANCE_CHECK_SEC; low-balance alert throttled to BALANCE_ALERT_SEC.
# BALANCE_FIRES = K fires of headroom at the default tip on top of one max-bid envelope: K=3 ≈ a
# cascade's back-to-back fires (profit is swept in the LOAN token — wins never refill gas ETH).
BALANCE_CHECK_SEC = float(os.environ.get("KT_BALANCE_CHECK_SEC", "600"))
BALANCE_ALERT_SEC = float(os.environ.get("KT_BALANCE_ALERT_SEC", "3600"))
BALANCE_FIRES = int(os.environ.get("KT_BALANCE_FIRES", "3"))
HEARTBEAT_SEC = int(os.environ.get("KT_HEARTBEAT_SEC", "86400"))
# In-process signing is the DEFAULT (review H3): the cast subprocess adds 0.5-2s to the fire
# path and its own RPC round-trips. cast remains as the KT_RAW_TX=0 fallback.
RAW_TX = os.environ.get("KT_RAW_TX", "1") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"
CHECKPOINT_BLOCK = os.environ.get("KT_CHECKPOINT_BLOCK")
# ETH/USD seed for gas math AND the non-stable (vbETH-loan) profit floor; refreshed from a live
# Sushi quote every ~5min by the loop (review H1) — the env value is only the cold-start seed.
ETH_USD = float(os.environ.get("KT_ETH_USD", "3300"))

# --- v3 latency upgrade: predictive block-boundary detect + pre-armed fire fast path ---
# MEASURED (probe harness ~/.katana-probe, 551 probes, 2026-07-16/17): a tx lands in the NEXT
# block only if it ARRIVES at the sequencer within ~0.25-0.35s of block N becoming visible —
# P(next) 21% at submit-offset +0.05s, 9-13% at +0.15-0.25s, ~0% at >=0.35s; send one-way
# ~110-150ms. The classic detect (fixed cadence + cold sockets, 0.65-1.05s) forfeited that
# window before even evaluating (end-to-end 1.5-2.1s -> P(B0+1) ~7-10%). Predictive mode
# (bot/fastpath.py) phase-locks the ~1.000s tick, does ALL maintenance (hot pass, Sushi
# re-quotes, preflight, pre-sign, warm-up pings) in the idle zone, and on boundary detect
# runs only: ONE pre-built multicall price read -> integer flip-threshold compare ->
# pre-signed eth_sendRawTransaction. Critical path ~ step/2 + 2 warm RTT (~60-110ms).
PREDICTIVE_POLL = os.environ.get("KT_PREDICTIVE_POLL", "1") == "1"
ARM_HF = float(os.environ.get("KT_ARM_HF", "1.002"))   # pre-arm 1 <= HF < this (near-flip)
ARM_MAX_N = int(os.environ.get("KT_ARM_MAX_N", "4"))   # biggest tickets first
ARM_QUOTE_TTL = float(os.environ.get("KT_ARM_QUOTE_TTL", "2.5"))  # armed quote/sig freshness
# Blind fire = skip quote+preflight inside the critical path (both ran at arm time). Safe at
# the default 0.001 gwei tip: a lost race reverts on-chain for ~$0.001. With a Phase-2 fee
# bid a reverted tx burns the BID, so any target the bidder would escalate keeps the free
# preflight eth_call (~1 warm RTT) in its critical path. KT_BLIND_FIRE=0 forces preflight
# for every fast fire.
BLIND_FIRE = os.environ.get("KT_BLIND_FIRE", "1") == "1"

# fire-path tuning (review H7/H8): tight quote timeouts, bounded receipt wait, short success-
# dedup so the REMAINDER of a chunked close is re-taken immediately instead of gifted for 5min.
QUOTE_TIMEOUT = float(os.environ.get("KT_QUOTE_TIMEOUT", "5"))
QUOTE_RETRIES = int(os.environ.get("KT_QUOTE_RETRIES", "2"))
EVAL_DEADLINE_SEC = float(os.environ.get("KT_EVAL_DEADLINE_SEC", "10"))
RECEIPT_WAIT_SEC = float(os.environ.get("KT_RECEIPT_WAIT_SEC", "20"))
DEDUP_OK_SEC = float(os.environ.get("KT_DEDUP_OK_SEC", "10"))

STATE_FILE = os.path.expanduser(os.environ.get("KT_STATE", "~/.katana-bot/exec_state.json"))
ENV_FILE = os.path.expanduser("~/.claude/channels/telegram/.env")
CHAT_ID = os.environ.get("KT_CHAT_ID", "")     # empty -> alerts disabled (loud preflight warn)

LIQUIDATE_SELECTOR = selector(
    "liquidate((address,address,address,address,uint256),address,uint256,uint256,"
    "address,bytes,uint256)")
SEL_ORACLE_PRICE = selector("price()")
_DEC = {v["address"].lower(): v["decimals"] for v in TOKENS.values()}
# chunk fractions tried, largest-first — the largest whose net clears the floor wins.
# RATIONALS, not floats: repaidShares on an 18-dec loan run ~1e27 (virtual-share scale 1e6),
# far past float64's 2^53 exact-int range — `int(shares * 0.75)` silently corrupts the number
# and Morpho's checked `borrowShares -= repaidShares` can Panic(0x11) the whole tx (review C1).
CHUNK_FRACTIONS = ((1, 1), (3, 4), (1, 2), (7, 20), (1, 4), (3, 20), (1, 10), (3, 50))
# swap-input haircut as a rational for the same reason (bot/sushi.py SWAP_INPUT_HAIRCUT = 0.003)
_HAIRCUT_NUM = 1000 - int(round(SWAP_INPUT_HAIRCUT * 1000))
_HAIRCUT_DEN = 1000


# --- state (kill-switch / dedup) ----------------------------------------------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"day": "", "gas_usd": 0.0, "consec_reverts": 0, "sent": {}, "declined": {},
            "last_heartbeat": 0, "passes": 0, "fires": 0, "reverts": 0, "races_lost": 0}


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    json.dump(st, open(tmp, "w"))
    os.replace(tmp, STATE_FILE)


def _roll_day(st: dict, today: str) -> None:
    if st.get("day") != today:
        st["day"] = today
        st["gas_usd"] = 0.0


# --- telegram (optional; loud preflight warning if not configured) -------------
_TG_TOKEN: str | None = None


def _tg_token() -> str:
    """Cached token: KT_TG_TOKEN env first, then the telegram channel env file."""
    global _TG_TOKEN
    if _TG_TOKEN is None:
        tok = os.environ.get("KT_TG_TOKEN", "")
        if not tok:
            try:
                with open(ENV_FILE) as f:
                    for ln in f:
                        if ln.startswith("TELEGRAM_BOT_TOKEN="):
                            tok = ln.split("=", 1)[1].strip()
            except OSError:
                tok = ""
        _TG_TOKEN = tok
    return _TG_TOKEN


def _alert_send(text: str, timeout: float = 5.0) -> None:
    token = _tg_token()
    if not token or not CHAT_ID:
        print(f"[alert disabled] {text}")
        return
    try:
        data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                   data=data), timeout=timeout)
    except Exception as e:
        print(f"alert fail: {e}")


def alert(text: str, sync: bool = False) -> None:
    """Fire-and-forget by default — an alert must NEVER sit between decision and broadcast
    (a slow Telegram round-trip used to block the fire path for up to 20s, review H2).
    sync=True only where the process exits right after (kill-switch, preflight)."""
    if sync:
        _alert_send(text)
    else:
        threading.Thread(target=_alert_send, args=(text,), daemon=True).start()


# --- decimals for unknown (non-registry) tokens -------------------------------
_SEL_DECIMALS = selector("decimals()")


def token_decimals(rpc: Rpc, addr: str) -> int:
    a = addr.lower()
    if a in _DEC:
        return _DEC[a]
    try:
        r = rpc.eth_call(addr, _SEL_DECIMALS)
        d = int(r, 16) if r and r != "0x" else 18
    except Exception:
        d = 18
    _DEC[a] = d
    return d


# --- gas -----------------------------------------------------------------------
def gas_cost_usd(rpc: Rpc) -> float:
    try:
        gp = rpc.gas_price()
    except Exception:
        gp = int(0.01 * 1e9)
    return GAS_UNITS_EST * gp / 1e18 * ETH_USD


# --- loan-token USD pricing (floors/gates; profit math itself is quote-based) --
_ETH_LIKE = {TOKENS["vbETH"]["address"].lower(), TOKENS["weETH"]["address"].lower()}
_eth_usd_ts = 0.0


def _loan_usd_px(addr: str) -> float | None:
    """USD per whole loan token: stables $1; vbETH/weETH via the refreshed ETH_USD; else None.
    Without this, non-stable loans (i.e. the flagship weETH/vbETH market) had NO usd floor —
    min profit degenerated to 1 wei and the on-chain floor to net//2 alone (review H1)."""
    a = addr.lower()
    if a in STABLES:
        return 1.0
    if a in _ETH_LIKE:
        return ETH_USD
    return None


def refresh_eth_usd() -> None:
    """Refresh ETH_USD from a live Sushi vbETH->vbUSDC quote (5min TTL). Feeds gas math and
    the vbETH-loan profit floor; the KT_ETH_USD env value is only the cold-start seed."""
    global ETH_USD, _eth_usd_ts
    if time.time() - _eth_usd_ts < 300:
        return
    _eth_usd_ts = time.time()          # even on failure — don't re-try every pass
    try:
        q = quote(TOKENS["vbETH"]["address"], TOKENS["vbUSDC"]["address"], 10 ** 18,
                  sender="0x000000000000000000000000000000000000dEaD",
                  recipient="0x000000000000000000000000000000000000dEaD",
                  max_slippage=0.005, timeout=QUOTE_TIMEOUT, retries=1)
        v = q["amount_out"] / 1e6
        if 100.0 < v < 100000.0:
            ETH_USD = v
    except Exception as e:
        print(f"eth_usd refresh fail: {e}")


# --- evaluate: size the exit against a live Sushi quote (chunking) ------------
def evaluate(rpc: Rpc, t: dict, gas_usd: float, deadline_mono: float | None = None) -> dict | None:
    """Quote the exit for target `t` (a monitor scan row), chunking down until the net clears
    KT_MIN_PROFIT_USD. Returns fire params (repaidShares, swapTarget, swapCalldata, minProfit)
    or None if no chunk is profitable. All chunk sizing is EXACT integer math (review C1).
    `deadline_mono` (optional) caps the internal deadline — the pre-arm path runs this inside
    the idle zone and must never let a slow Sushi chain overrun the armed window."""
    loan, coll = t["loan"], t["coll"]
    loan_dec = token_decimals(rpc, loan)
    lif = lif_from_lltv(t["lltv"])
    seized_full = t["seized_assets"]
    repaid_full = t["repaid_assets"]                # loan assets repaid at full close
    shares_full = t["borrow_shares_repaid"]         # LIVE borrowShares scaled to the close
    # collateral-capped close (deep underwater, repaid bounded by collateral value / LIF):
    # fire in seizedAssets mode — we pin the seize, Morpho derives repaid at execution price,
    # so a price tick between scan and inclusion can never Panic(0x11) (review M2).
    capped = repaid_full < t["debt_assets"]
    if seized_full <= 0 or (not capped and shares_full <= 0):
        return None
    loan_px = _loan_usd_px(loan)
    # USD profit floor converted into loan wei (stables: $1/token as before; vbETH via ETH_USD)
    usd_floor_wei = max(1, int(MIN_PROFIT_USD / loan_px * 10 ** loan_dec)) if loan_px else 1

    best = None
    deadline = time.monotonic() + EVAL_DEADLINE_SEC
    if deadline_mono is not None:
        deadline = min(deadline, deadline_mono)
    for num, den in CHUNK_FRACTIONS:
        if time.monotonic() > deadline:
            # checked BEFORE each quote: a spent budget must never start another network
            # round-trip (the post-failure checks below only cover the failure paths)
            print("    evaluate deadline exceeded")
            break
        seized = seized_full * num // den
        if seized <= 0:
            continue
        if capped:
            # 0.3% under the cap so the price-derived repaid keeps headroom vs remaining debt;
            # the swap input IS the pinned seize — exact, no drift, no extra haircut needed
            seized_arg = seized * _HAIRCUT_NUM // _HAIRCUT_DEN
            amount_in = seized_arg
        else:
            seized_arg = 0
            amount_in = seized * _HAIRCUT_NUM // _HAIRCUT_DEN
        try:
            q = quote(coll, loan, amount_in,
                      sender=CONTRACT or "0x000000000000000000000000000000000000dEaD",
                      recipient=CONTRACT or "0x000000000000000000000000000000000000dEaD",
                      max_slippage=MAX_SLIPPAGE, timeout=QUOTE_TIMEOUT, retries=QUOTE_RETRIES)
        except NoRouteError:
            # no route at any size (dead/exotic collateral, e.g. yUSD) — skip this target
            return None
        except Exception as e:
            print(f"    quote fail f={num}/{den}: {e}")
            if time.monotonic() > deadline:
                print("    evaluate deadline exceeded, giving up this pass")
                return None
            continue
        proceeds = q["amount_out"]
        if capped:
            # repaid is derived by Morpho at execution price; estimate it (ceil, against us)
            # from the scan price for the profit gate — tx params carry no repaid at all
            repaid = int(seized_arg * t["price"] / 10 ** 36 / lif) + 1
        else:
            # chunk amounts scale EXACTLY with the fraction (floor, against ourselves)
            repaid = repaid_full * num // den
        net_wei = proceeds - repaid
        net_usd = (net_wei / 10 ** loan_dec * loan_px) - gas_usd if loan_px else None
        row = {"f": num / den, "seized": seized, "repaid_assets": repaid,
               "repaid_shares": 0 if capped else shares_full * num // den,
               "seized_arg": seized_arg,
               "proceeds": proceeds, "net_wei": net_wei, "net_usd": net_usd,
               "impact": q["price_impact"], "swap_target": q["swap_target"],
               "swap_calldata": q["swap_calldata"], "loan_dec": loan_dec}
        profitable = net_wei >= usd_floor_wei and q["price_impact"] <= MAX_IMPACT
        if net_usd is not None:
            profitable = profitable and net_usd >= MIN_PROFIT_USD
        if profitable:
            best = row
            break   # largest profitable chunk (fractions are descending)
        if time.monotonic() > deadline:
            print("    evaluate deadline exceeded")
            break
    if best is None:
        return None
    # on-chain minProfit floor (2nd safety layer): the USD floor AND at least half the quoted
    # net — a stale/optimistic mid-quote may realise worse, but never let >50% of the promised
    # edge evaporate silently (review H1/M1; the preflight eth_call makes a doomed tx free).
    best["min_profit_wei"] = max(usd_floor_wei, best["net_wei"] // 2)
    best["lif"] = lif
    return best


# --- calldata + signing --------------------------------------------------------
def liquidate_calldata(t: dict, ev: dict) -> str:
    """Exactly one of ev[seized_arg]/ev[repaid_shares] is nonzero (Morpho argument order):
    repaidShares mode for debt-bound closes, seizedAssets mode for collateral-capped ones
    (Morpho derives repaid at execution price — no Panic(0x11) on an adverse tick, M2)."""
    from eth_abi import encode
    types = ["(address,address,address,address,uint256)", "address", "uint256", "uint256",
             "address", "bytes", "uint256"]
    mp = (_cs(t["loan"]), _cs(t["coll"]), _cs(t["oracle"]), _cs(t["irm"]), t["lltv"])
    args = [mp, _cs(t["borrower"]), ev.get("seized_arg", 0), ev["repaid_shares"],
            _cs(ev["swap_target"]), bytes.fromhex(ev["swap_calldata"][2:]),
            ev["min_profit_wei"]]
    return LIQUIDATE_SELECTOR + encode(types, args).hex()


def _cs(addr: str) -> str:
    try:
        from eth_utils import to_checksum_address
        return to_checksum_address(addr)
    except Exception:
        return addr


# raw-tx write client (separate from the read-only Rpc whitelist) over a KEPT-ALIVE
# connection — a fresh TCP+TLS handshake per call costs 2-3 RTT and the fire path stacks
# several calls back-to-back (review H9). Transparent reconnect on a stale socket; on a
# transport failure the endpoint rotates to the next KT_WRITE_RPCS fallback (if any).
_WRITE_URLS = [urllib.parse.urlsplit(u) for u in WRITE_RPCS]
_write_conn: http.client.HTTPConnection | None = None
_write_idx = 0


class RpcTransportError(RuntimeError):
    """Write-RPC failure that says NOTHING about the target: timeout/refused/garbage body, or a
    server-side JSON-RPC error (rate limit {"code":-32005}, internal, ...). Callers must back
    off + retry — NEVER classify it as an execution revert or decline the target on it (a
    rate-limited write RPC during a cascade would otherwise self-ban every profitable target)."""


# JSON-RPC errors that ARE a genuine execution revert of our eth_call/tx: EIP-1474 code 3
# (execution error, revert data attached), legacy -32015 "vm execution error", or a body
# carrying "execution reverted"/revert data. EVERYTHING else — rate limits ({"code":-32005}),
# -32603 internals, "insufficient funds", nonce noise — is NOT a verdict on the target.
_REVERT_CODES = {3, -32015}


def _is_revert_error(err) -> bool:
    if not isinstance(err, dict):
        return "execution reverted" in str(err).lower()
    if err.get("code") in _REVERT_CODES:
        return True
    msg = str(err.get("message", "")).lower()
    data = err.get("data")
    return ("execution reverted" in msg or "revert" in msg
            or (isinstance(data, str) and data.startswith("0x") and len(data) > 2))


def _rpc_write(method: str, params: list, timeout: float = 15.0):
    global _write_conn, _write_idx
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    d = last = None
    for _ in range(max(2, len(_WRITE_URLS))):   # >=1 inline retry; each fallback gets one shot
        u = _WRITE_URLS[_write_idx % len(_WRITE_URLS)]
        try:
            if _write_conn is None:
                cls = (http.client.HTTPSConnection if u.scheme == "https"
                       else http.client.HTTPConnection)
                _write_conn = cls(u.netloc, timeout=timeout)
            _write_conn.request("POST", u.path or "/", body,
                                {"Content-Type": "application/json",
                                 "User-Agent": "Mozilla/5.0"})
            d = json.loads(_write_conn.getresponse().read())
            break
        except (OSError, http.client.HTTPException, ValueError) as e:
            last = e
            try:
                _write_conn.close()
            except Exception:
                pass
            _write_conn = None
            _write_idx += 1     # rotate to the next write endpoint (no-op with a single one)
    if d is None:
        raise RpcTransportError(f"rpc {method} transport: {last}") from last
    if d.get("error"):
        err = d["error"]
        if _is_revert_error(err):
            raise RuntimeError(f"rpc {method}: {err}")
        raise RpcTransportError(f"rpc {method}: {err}")
    return d["result"]


def _warm_write() -> None:
    """Keep the write lane's socket open with a cheap eth_chainId (~1 warm RTT): the LB idle
    timeout (~60s) would otherwise leave eth_sendRawTransaction paying TCP+TLS exactly when a
    target flips. Called every hot pass and before each armed window. Never raises."""
    try:
        _rpc_write("eth_chainId", [])
    except Exception:
        pass


_owner_addr_cache: str | None = None


def _owner_address() -> str | None:
    """Bot EOA derived from the key (cached); None if eth_account is unavailable."""
    global _owner_addr_cache
    if _owner_addr_cache is None:
        try:
            from eth_account import Account
            _owner_addr_cache = Account.from_key(PRIVATE_KEY).address if PRIVATE_KEY else ""
        except Exception:
            _owner_addr_cache = ""
    return _owner_addr_cache or None


def _competitive_priority_gwei(net_usd) -> float:
    """Phase 2: priority fee (gwei) to bid on a CONTESTED ticket — competitive with Katana's
    priority-gas auction (measured 171-443 gwei) but capped so we still keep >= FEE_BID_KEEP_USD of
    net after paying it. Returns the default PRIORITY_GWEI when fee-bidding is off / ticket too small.
    net_usd is post-base-gas quoted net; the affordable bid burns (net - keep) into the tip."""
    if not FEE_BID or net_usd is None or net_usd < FEE_BID_MIN_NET_USD:
        return PRIORITY_GWEI
    denom = GAS_UNITS_EST * ETH_USD / 1e9          # USD cost of 1 gwei of tip at GAS_UNITS_EST
    affordable = (net_usd - FEE_BID_KEEP_USD) / denom if denom > 0 else 0.0
    if affordable <= PRIORITY_GWEI:
        return PRIORITY_GWEI
    return min(MAX_PRIORITY_GWEI, affordable)


def _fee_params(priority_gwei: float | None = None) -> tuple[int, int]:
    priority = int((priority_gwei if priority_gwei is not None else PRIORITY_GWEI) * 1e9)
    try:
        blk = _rpc_write("eth_getBlockByNumber", ["latest", False])
        base = int(blk["baseFeePerGas"], 16)
        return base * 2 + priority, priority
    except Exception:
        gp = int(_rpc_write("eth_gasPrice", []), 16)
        return gp + priority, priority


# --- revert classification (review H5) -----------------------------------------
# Morpho core reverts with Error(string) ("position is healthy", ...) or Panic(0x11) on
# checked-arith over-repay/over-seize after a competitor's close. BOTH mean "lost the race",
# NOT a bot defect — they must not feed the kill-switch (3 lost races in a cascade would stop
# the bot exactly when it matters).
_PANIC_SEL_HEX = "4e487b71"


def _is_lost_race(err_text: str) -> bool:
    s = err_text.lower()
    return "healthy" in s or _PANIC_SEL_HEX in s or "panic" in s


def _preflight_call(calldata: str) -> tuple[bool, str]:
    """eth_call the EXACT liquidation tx before broadcasting: runs Morpho's real accrual +
    _isHealthy, so a lost race / stale calldata reverts here in ~10ms for zero gas instead of
    on-chain (review H5/C3). Returns (ok, revert_text) for a GENUINE revert; a transport /
    rate-limit failure propagates as RpcTransportError — the caller backs off + retries, it
    must never be mistaken for a revert verdict on the target."""
    call = {"to": _cs(CONTRACT), "data": calldata, "gas": hex(GAS_LIMIT)}
    frm = _owner_address()
    if frm:
        call["from"] = frm
    try:
        _rpc_write("eth_call", [call, "latest"])
        return True, ""
    except RpcTransportError:
        raise
    except RuntimeError as e:
        return False, str(e)


def _settle(st: dict, key: str, txh: str, rcpt: dict, now_ts: float,
            gas_est_usd: float, calldata: str | None) -> str:
    """Classify a mined receipt: ok / lost_race / revert; swap the pre-charged gas estimate
    for the receipt's actual cost. Returns a short outcome string for the alert."""
    try:   # actual gas from the receipt, not the estimate (review: gas accounting)
        actual = (int(rcpt["gasUsed"], 16) * int(rcpt.get("effectiveGasPrice", "0x0"), 16)
                  / 1e18 * ETH_USD)
        st["gas_usd"] += actual - gas_est_usd
    except Exception:
        pass
    if int(rcpt.get("status", "0x0"), 16) == 1:
        _record(st, key, txh, now_ts, "ok")
        return "ok"
    why = ""
    if calldata:   # replay at the mined block to classify the revert
        call = {"to": _cs(CONTRACT), "data": calldata, "gas": hex(GAS_LIMIT)}
        if _owner_address():
            call["from"] = _owner_address()
        try:
            _rpc_write("eth_call", [call, rcpt.get("blockNumber", "latest")])
        except RuntimeError as e:
            why = str(e)
    status = "lost_race" if _is_lost_race(why) else "revert"
    _record(st, key, txh, now_ts, status)
    return f"{status} {why[:160]}"


def _record_send_error(st: dict, key: str, now_ts: float, gas_usd: float, e, what: str) -> None:
    """A failed send (insufficient funds / bad nonce / RPC down mid-send) is NOT a revert
    (review M10): refund the gas estimate, don't feed the kill-switch. But it must not retry at
    hot-tick cadence either — the send_error journal entry cools the target down for
    SEND_ERR_COOLDOWN_SEC (see recently_fired) and the alert is throttled per-target
    (SEND_ERR_ALERT_SEC) + globally (SEND_ERR_ALERT_GLOBAL_SEC): an unfunded wallet used to
    re-quote + re-fire + alert EVERY ~1s hot tick, per target, until topped up."""
    st["gas_usd"] -= gas_usd
    st["sent"][key] = {"tx": f"senderr:{e}"[:200], "ts": now_ts, "status": "send_error"}
    seen = st.setdefault("send_err_alerted", {})
    if (now_ts - seen.get(key, 0) > SEND_ERR_ALERT_SEC
            and now_ts - st.get("last_send_err_alert", 0) > SEND_ERR_ALERT_GLOBAL_SEC):
        seen[key] = now_ts
        st["last_send_err_alert"] = now_ts
        alert(f"⚠️ {what} error (not counted as revert; target cooldown "
              f"{SEND_ERR_COOLDOWN_SEC:.0f}s): {str(e)[:200]}")
    else:
        print(f"  {what} error (alert throttled): {str(e)[:200]}")
    for k in [k for k, ts in seen.items() if now_ts - ts > 86400]:   # prune
        del seen[k]


def _sign_liquidate(nonce: int, max_fee: int, priority: int, calldata: str) -> str:
    """EIP-1559 sign of the liquidate tx -> raw 0x-hex. Shared by the classic path (signs at
    send time) and the pre-arm path (signs in the idle window; the signature freezes nonce +
    fee, which is why armed entries are invalidated whenever another fire happens)."""
    from eth_account import Account
    tx = {"chainId": CHAIN_ID, "nonce": nonce, "to": _cs(CONTRACT), "value": 0,
          "gas": GAS_LIMIT, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority,
          "data": calldata}
    signed = Account.sign_transaction(tx, PRIVATE_KEY)     # key never logged
    raw = signed.raw_transaction
    return raw.to_0x_hex() if hasattr(raw, "to_0x_hex") else "0x" + raw.hex()


def _fire_raw(t: dict, ev: dict, st: dict, now_ts: float, key: str, calldata: str,
              gas_usd: float, priority_gwei: float | None = None) -> None:
    from eth_account import Account
    try:
        addr = Account.from_key(PRIVATE_KEY).address
        max_fee, priority = _fee_params(priority_gwei)
        nonce = int(_rpc_write("eth_getTransactionCount", [addr, "pending"]), 16)
        raw_hex = _sign_liquidate(nonce, max_fee, priority, calldata)
        txh = _rpc_write("eth_sendRawTransaction", [raw_hex])
    except Exception as e:
        # transport/signing failure BEFORE broadcast is NOT a revert (review M10): refund the
        # gas estimate, don't feed the kill-switch; cooldown + throttled alert.
        _record_send_error(st, key, now_ts, gas_usd, e, "send")
        return
    _post_broadcast(t, ev, st, now_ts, key, calldata, gas_usd, txh)


def _post_broadcast(t: dict, ev: dict, st: dict, now_ts: float, key: str, calldata: str,
                    gas_usd: float, txh: str) -> None:
    """Post-send tracking shared by the classic and pre-armed fast paths: alert strictly
    AFTER broadcast (review H2), bounded receipt wait, settle — or track as pending (H7)."""
    alert(f"🔫 sent {txh} HF={t['hf']:.4f} chunk={ev['f']:.0%} {t['borrower'][:10]}…")
    rcpt, deadline = None, time.time() + RECEIPT_WAIT_SEC
    while time.time() < deadline:
        try:
            rcpt = _rpc_write("eth_getTransactionReceipt", [txh])
        except RuntimeError:
            rcpt = None
        if rcpt:
            break
        time.sleep(1.0)
    if not rcpt:
        # keep the loop hot: track the pending tx, settle on later passes (review H7) — a
        # receipt timeout is NOT a revert (the tx may still mine)
        st["sent"][key] = {"tx": txh, "ts": now_ts, "status": "pending",
                           "calldata": calldata, "gas_est": gas_usd}
        alert(f"⏳ no receipt in {RECEIPT_WAIT_SEC:.0f}s, tracking: {txh}")
        return
    outcome = _settle(st, key, txh, rcpt, now_ts, gas_usd, calldata)
    icon = "✅" if outcome == "ok" else ("🏁" if outcome.startswith("lost_race") else "❌")
    alert(f"{icon} {outcome}: {txh}")


def _fire_cast(t: dict, ev: dict, st: dict, now_ts: float, key: str, calldata: str,
               gas_usd: float, priority_gwei: float | None = None) -> None:
    # fallback path (KT_RAW_TX=0). Key via env, NOT argv — argv is world-readable in
    # /proc/*/cmdline for the whole cast run (review H3).
    pg = priority_gwei if priority_gwei is not None else PRIORITY_GWEI
    args = ["cast", "send", CONTRACT, calldata, "--gas-limit", str(GAS_LIMIT),
            "--priority-gas-price", str(int(pg * 1e9)), "--rpc-url", RPC_WRITE]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, ETH_PRIVATE_KEY=PRIVATE_KEY))
        out = (r.stdout or "") + (r.stderr or "")
        reverted = ("status" in out and "0 (failed)" in out) or (r.returncode != 0)
        status = "revert" if reverted else "ok"
        if reverted and _is_lost_race(out):
            status = "lost_race"
        _record(st, key, out[-80:].strip(), now_ts, status)
        alert(f"{'✅ liq ok' if status == 'ok' else '❌ ' + status}: {out[-300:]}")
    except Exception as e:
        _record_send_error(st, key, now_ts, gas_usd, e, "cast")


def _record(st: dict, key: str, tx: str, now_ts: float, status: str) -> None:
    st["sent"][key] = {"tx": tx, "ts": now_ts, "status": status}
    if status == "revert":
        st["consec_reverts"] += 1
        st["reverts"] += 1
    elif status == "ok":
        st["consec_reverts"] = 0
    elif status == "lost_race":
        st["races_lost"] = st.get("races_lost", 0) + 1


def _check_pending(st: dict, now_ts: float) -> None:
    """Settle still-pending txs from previous passes (review H7): reclassify once mined; after
    10min unmined, mark stale + alert (possible stuck nonce) — never counted as a revert."""
    for key, rec in list(st["sent"].items()):
        if rec.get("status") != "pending" or not str(rec.get("tx", "")).startswith("0x"):
            continue
        try:
            rcpt = _rpc_write("eth_getTransactionReceipt", [rec["tx"]])
        except Exception:
            continue
        if rcpt:
            outcome = _settle(st, key, rec["tx"], rcpt, rec["ts"],
                              rec.get("gas_est", 0.0), rec.get("calldata"))
            alert(f"📬 pending settled — {outcome}: {rec['tx']}")
        elif now_ts - rec["ts"] > 600:
            rec["status"] = "stale"
            alert(f"⚠️ tx unmined for 10min (stuck nonce? fee snapshot too low?): {rec['tx']}")
    # prune the journal so it can't grow unbounded
    for key, rec in list(st["sent"].items()):
        if now_ts - rec.get("ts", 0) > 86400:
            del st["sent"][key]


def fire(rpc: Rpc, t: dict, ev: dict, st: dict, now_ts: float, gas_usd: float) -> None:
    key = f"{t['market_id']}:{t['borrower']}"
    nets = f"${ev['net_usd']:+,.1f}" if ev["net_usd"] is not None else f"{ev['net_wei']} wei"
    # Phase 2: competitive priority bid on a big contested ticket (default off -> == PRIORITY_GWEI)
    bid_gwei = _competitive_priority_gwei(ev["net_usd"])
    bid_note = f" bid={bid_gwei:.0f}gwei" if bid_gwei > PRIORITY_GWEI else ""
    if DRY_RUN or not CONTRACT:
        mode = (f"seizedAssets={ev.get('seized_arg', 0)}" if ev.get("seized_arg")
                else f"repaidShares={ev['repaid_shares']}")
        msg = (f"🧪 DRY_RUN: HF={t['hf']:.4f} chunk={ev['f']:.0%} {mode} "
               f"net={nets}{bid_note} impact={ev['impact']*100:.2f}% mkt={t['market_id'][:10]} "
               f"{t['borrower'][:10]}…; NOT sent.")
        print(msg)
        return
    # re-read the oracle just before firing (review M3): seized was sized at scan-time price;
    # a drop beyond what the swap haircut absorbs would make the baked amountIn over-pull.
    try:
        pr = rpc.eth_call(t["oracle"], SEL_ORACLE_PRICE)
        p_now = int(pr, 16) if pr and pr != "0x" else 0
        if p_now and p_now < t["price"] * 998 // 1000:
            print(f"  price moved {(1 - p_now / t['price']) * 100:.2f}% down since scan — "
                  f"deferring one tick for a fresh scan")
            return
    except Exception:
        pass
    calldata = liquidate_calldata(t, ev)
    try:
        ok, why = _preflight_call(calldata)
    except RpcTransportError as e:
        # transport/rate-limit is NOT a verdict on the target: declining it for DECLINE_TTL
        # would self-ban every profitable target exactly during a cascade. Short per-target
        # backoff (ttl override) so the hot loop doesn't hammer the endpoint; retry next tick.
        st["declined"][key] = {"ts": now_ts, "ttl": TRANSIENT_BACKOFF_SEC}
        print(f"  preflight transport (backoff {TRANSIENT_BACKOFF_SEC:.0f}s, retrying — "
              f"NOT declined): {str(e)[:160]}")
        return
    if not ok:
        if _is_lost_race(why):
            st["races_lost"] = st.get("races_lost", 0) + 1
            msg = (f"🏁 preflight: lost race / healthy again {t['borrower'][:10]}… "
                   f"HF={t['hf']:.4f} ({why[:120]})")
            print(f"  {msg}")
            alert(msg)
        else:
            print(f"  preflight revert (NOT sent, zero gas): {why[:200]}")
        st["declined"][key] = {"ts": now_ts}
        return
    fire_gas_usd = gas_usd
    if bid_gwei > PRIORITY_GWEI:
        # charge the elevated win-cost to the kill-switch UP FRONT (conservative: a lost bid only
        # pays reverted gas, so this over-counts losses — trips the daily cap sooner, never later)
        fire_gas_usd = GAS_UNITS_EST * bid_gwei / 1e9 * ETH_USD
        print(f"  💸 fee-bid {bid_gwei:.0f} gwei (net {nets} > ${FEE_BID_MIN_NET_USD:.0f}); "
              f"est win-cost ${fire_gas_usd:,.0f}, keeps ~${(ev['net_usd'] or 0) - fire_gas_usd:,.0f}")
    st["fires"] += 1
    st["gas_usd"] += fire_gas_usd
    if RAW_TX:
        _fire_raw(t, ev, st, now_ts, key, calldata, fire_gas_usd, bid_gwei)
    else:
        _fire_cast(t, ev, st, now_ts, key, calldata, fire_gas_usd, bid_gwei)


# --- pre-armed fire fast path (KT_PREDICTIVE_POLL; see fastpath docstring) ------
# key -> armed entry {t, ev, key, calldata, raw, blind, bid_gwei, gas_usd, ts (monotonic),
# fires_at_sign} or {skip_until} for targets evaluate() declined at arm time (don't re-quote
# the perpetual near-edge dregs every idle window). Module-level: the loop is single-threaded.
_arm: dict[str, dict] = {}


def _arm_candidates(rows: list[dict]) -> list[dict]:
    """Near-flip rows worth pre-arming: healthy but within KT_ARM_HF of the line, past the
    same MIN_DEBT gate once() applies to targets, biggest debt first, capped at ARM_MAX_N."""
    cand = [r for r in rows if 1.0 <= r["hf"] < ARM_HF
            and (r["debt_usd"] is None or r["debt_usd"] >= MIN_DEBT_USD)]
    return sorted(cand, key=lambda r: -(r.get("debt_usd") or 0))[:ARM_MAX_N]


def _arm_refresh(rpc: Rpc, rows: list[dict], st: dict, now_ts: float,
                 deadline_mono: float) -> None:
    """Idle-zone maintenance: (re)build pre-armed fire entries for near-flip targets.
    Everything the classic path does between detect and broadcast — live shares, the Sushi-
    quoted sizing (evaluate(): IDENTICAL floors/chunk math, economics unchanged), calldata,
    a sanity preflight, fee params, nonce, signature — happens HERE under a time budget, so
    a flip's critical path is just: threshold compare (µs) + eth_sendRawTransaction.
    Entries go stale after ARM_QUOTE_TTL — a fast fire must never use an old quote."""
    mono = time.monotonic()
    cands = _arm_candidates(rows)
    keep = {f"{r['market_id']}:{r['borrower']}" for r in cands}
    for k in list(_arm):    # prune cured/out-of-set entries (keep unexpired skip records)
        if k not in keep and _arm[k].get("skip_until", 0) <= now_ts:
            del _arm[k]
    gas_usd = None
    for t in cands:
        key = f"{t['market_id']}:{t['borrower']}"
        e = _arm.get(key)
        if e and e.get("skip_until", 0) > now_ts:
            continue
        if e and e.get("ev") and mono - e["ts"] < ARM_QUOTE_TTL * 0.5:
            continue        # fresh enough — don't burn idle budget re-quoting
        if time.monotonic() > deadline_mono - 0.30:
            break           # a quote round needs ~0.3s headroom — never START one that will
            #                 overrun the armed window (a started quote can't be cancelled)
        if recently_fired(st, key, now_ts) or recently_declined(st, key, now_ts):
            continue
        t = dict(t)         # never mutate the caller's scan rows
        try:
            t["borrow_shares_repaid"] = (0 if t["repaid_assets"] < t["debt_assets"]
                                         else _shares_for_repaid(rpc, t))
            if gas_usd is None:
                gas_usd = gas_cost_usd(rpc)
            ev = evaluate(rpc, t, gas_usd, deadline_mono=deadline_mono)
        except Exception as err:
            print(f"  arm {t['borrower'][:10]}…: {str(err)[:120]}")
            continue
        if not ev:
            if time.monotonic() > deadline_mono:
                continue    # the idle BUDGET stopped it, not economics — retry next window
            # same verdict evaluate() gives flipped targets (no profitable chunk/no route).
            # NOT st['declined'] — the target hasn't flipped; a local skip TTL instead.
            _arm[key] = {"skip_until": now_ts + DECLINE_TTL}
            continue
        bid_gwei = _competitive_priority_gwei(ev["net_usd"])
        entry = {"t": t, "ev": ev, "key": key, "calldata": liquidate_calldata(t, ev),
                 "ts": time.monotonic(), "raw": None, "bid_gwei": bid_gwei,
                 "blind": BLIND_FIRE and bid_gwei <= PRIORITY_GWEI,
                 "gas_usd": (GAS_UNITS_EST * bid_gwei / 1e9 * ETH_USD
                             if bid_gwei > PRIORITY_GWEI else gas_usd)}
        if not DRY_RUN and CONTRACT and PRIVATE_KEY:
            try:
                pf_ok, why = _preflight_call(entry["calldata"])
            except RpcTransportError as err:
                pf_ok, why = None, str(err)   # unknown — arm, but keep preflight in the path
            if pf_ok:
                # already liquidatable (crossed between scan and now): fire the classic path
                # right here — it re-checks the oracle and preflights again itself
                ok, reason = guard_ok(st)
                if not ok:
                    raise GuardTripped(reason)
                print(f"  arm: {t['borrower'][:10]}… already flipped — classic fire now")
                fire(rpc, t, ev, st, now_ts, gas_usd)
                continue
            if pf_ok is False and "healthy" not in why.lower():
                # the arm-time preflight of a not-yet-flipped target MUST revert 'position
                # is healthy' — that proves calldata/route reach Morpho intact. Any OTHER
                # revert (SwapFailed, Panic on current state, ...) = do NOT arm this build.
                print(f"  arm: unexpected preflight revert (not armed): {why[:120]}")
                _arm[key] = {"skip_until": now_ts + TRANSIENT_BACKOFF_SEC}
                continue
            if pf_ok is None:
                entry["blind"] = False        # transport blinded the sanity check
            try:
                from eth_account import Account
                addr = Account.from_key(PRIVATE_KEY).address
                max_fee, priority = _fee_params(entry["bid_gwei"])
                nonce = int(_rpc_write("eth_getTransactionCount", [addr, "pending"]), 16)
                entry["raw"] = _sign_liquidate(nonce, max_fee, priority, entry["calldata"])
                entry["fires_at_sign"] = st.get("fires", 0)
            except Exception as err:
                print(f"  arm sign failed (classic path covers): {str(err)[:120]}")
                continue
        _arm[key] = entry


def _fire_fast(entry: dict, st: dict, now_ts: float) -> bool:
    """Armed-window critical path for a flipped pre-armed target. Blind entries (default
    tip) go straight to eth_sendRawTransaction on the warm write lane — zero further RPC; a
    lost race reverts on-chain for ~$0.001. Bid entries keep the free preflight eth_call
    (~1 warm RTT): a reverted bid burns the bid. The classic path's oracle re-read (M3) is
    unnecessary here: this window fired BECAUSE of a price read this instant, and for the
    shares-mode closes armed entries always are, net is price-insensitive (repaid + swap
    amountIn are frozen in calldata; a lower exec price only seizes MORE collateral, surplus
    swept as dust) with the on-chain minProfit floor as the final guarantee.
    Returns True iff a broadcast happened (the caller then defers to the classic pass for
    settle/remainder). Never records send_error cooldowns — on any miss the classic pass
    retries with fresh nonce/fees within ~1s."""
    key, t, ev = entry["key"], entry["t"], entry["ev"]
    ok, reason = guard_ok(st)
    if not ok:
        raise GuardTripped(reason)
    if recently_fired(st, key, now_ts) or recently_declined(st, key, now_ts):
        return False
    if time.monotonic() - entry.get("ts", 0) > ARM_QUOTE_TTL:
        return False                      # stale quote/signature — classic pass takes it
    if DRY_RUN or not CONTRACT or not entry.get("raw"):
        nets = (f"${ev['net_usd']:+,.1f}" if ev["net_usd"] is not None
                else f"{ev['net_wei']}wei")
        print(f"  ⚡ fast-path flip {t['borrower'][:10]}… HF was {t['hf']:.4f} net={nets} "
              f"({'DRY_RUN' if DRY_RUN else 'unsigned'}; classic pass takes it)")
        return False
    if entry.get("fires_at_sign") != st.get("fires", 0):
        return False                      # a fire happened since signing: nonce is burned
    if not entry["blind"]:
        try:
            pf_ok, why = _preflight_call(entry["calldata"])
        except RpcTransportError as e:
            st["declined"][key] = {"ts": now_ts, "ttl": TRANSIENT_BACKOFF_SEC}
            print(f"  fast preflight transport (backoff, NOT declined): {str(e)[:120]}")
            return False
        if not pf_ok:
            if _is_lost_race(why):
                st["races_lost"] = st.get("races_lost", 0) + 1
                alert(f"🏁 fast preflight: lost race {t['borrower'][:10]}… ({why[:100]})")
            else:
                print(f"  fast preflight revert (NOT sent, zero gas): {why[:160]}")
            st["declined"][key] = {"ts": now_ts}
            return False
    st["fires"] += 1
    st["gas_usd"] += entry["gas_usd"]
    try:
        txh = _rpc_write("eth_sendRawTransaction", [entry["raw"]])
    except Exception as e:
        st["fires"] -= 1                  # refund — the classic pass takes over untainted
        st["gas_usd"] -= entry["gas_usd"]
        entry["raw"] = None
        print(f"  fast send failed (classic path takes over): {str(e)[:160]}")
        return False
    entry["raw"] = None                   # nonce consumed — never reusable
    _post_broadcast(t, ev, st, now_ts, key, entry["calldata"], entry["gas_usd"], txh)
    return True


def _predictive_cycle(read_rpc: Rpc, clock: "fastpath.BlockClock", rows: list[dict],
                      st: dict) -> bool:
    """One block-locked cycle around the hot pass: sync if needed -> idle-zone maintenance
    (arm refresh, warm-up pings, pre-built price calldata) -> boundary tight-poll -> ONE
    multicall price refresh -> flip-threshold compare -> pre-armed fire. Returns True when
    the boundary wait consumed this iteration's sleep (the loop runs the next hot pass
    immediately — that pass is the safety net for flips without an armed entry); False when
    the phase lock failed and the caller should fall back to the classic HOT_POLL cadence."""
    if not clock.synced and clock.sync() is None:
        return False                      # tight lane not answering — classic cadence
    now_ts = time.time()
    budget = time.monotonic() + max(0.0, clock.idle_remaining() - 0.15)
    _arm_refresh(read_rpc, rows, st, now_ts, budget)
    watch = fastpath.attach_flip_thresholds(rows)
    calldata, oracles = fastpath.build_price_refresh(watch) if watch else ("", [])
    read_rpc.warm()
    _warm_write()
    if clock.wait_next() is None:
        # soft break (still synced: late entry absorbed by a predicted anchor) -> True, the
        # loop runs the hot pass immediately and stays block-locked; hard break -> False,
        # fall back to the classic cadence and re-sync next cycle.
        return clock.synced
    if not watch:
        return True                       # block-locked cadence, nothing near the line
    try:
        ret = read_rpc.eth_call(MULTICALL3, calldata, gas=30_000_000)
        prices = fastpath.decode_price_refresh(ret, oracles)
    except Exception as e:
        print(f"  armed price refresh failed: {str(e)[:120]}")
        return True
    fired = False
    for row in fastpath.flipped(watch, prices):
        key = f"{row['market_id']}:{row['borrower']}"
        entry = _arm.get(key)
        if entry and entry.get("ev"):
            fired = _fire_fast(entry, st, time.time()) or fired
        else:
            print(f"  flip w/o armed entry {row['borrower'][:10]}… — classic pass takes it")
    if fired:
        save_state(st)
    return True


# --- guards --------------------------------------------------------------------
class GuardTripped(Exception):
    pass


def guard_ok(st: dict) -> tuple[bool, str]:
    if st["consec_reverts"] >= MAX_CONSEC_REVERTS:
        return False, f"{st['consec_reverts']} consecutive reverts >= {MAX_CONSEC_REVERTS}"
    if st["gas_usd"] >= MAX_DAILY_GAS_USD:
        return False, f"daily gas ${st['gas_usd']:.2f} >= ${MAX_DAILY_GAS_USD}"
    return True, ""


def recently_fired(st: dict, key: str, now_ts: float) -> bool:
    """Dedup policy (review H6): block while a tx is IN-FLIGHT (pending); after a confirmed
    success block only briefly (DEDUP_OK_SEC) — the next pass re-reads live borrowShares, so
    the REMAINDER of a chunked close is re-taken immediately instead of gifted to competitors
    for 5min (Morpho has no close factor). Reverts/lost races retry at once — real reverts are
    capped by the consec-reverts guard; send errors (unfunded wallet, RPC down mid-send) cool
    down for SEND_ERR_COOLDOWN_SEC — they'd otherwise re-run the full evaluate + alert every
    hot tick until the condition clears."""
    rec = st["sent"].get(key)
    if not rec:
        return False
    age = now_ts - rec["ts"]
    status = rec.get("status")
    if status == "pending":
        return age < DEDUP_SEC
    if status == "ok":
        return age < DEDUP_OK_SEC
    if status == "send_error":
        return age < SEND_ERR_COOLDOWN_SEC
    return False


def recently_declined(st: dict, key: str, now_ts: float) -> bool:
    """True if this target was declined ('no profitable chunk') within DECLINE_TTL — skip re-quoting
    it (avoids hammering Sushi with the perpetual bad-debt dregs at hot-poll cadence). A record may
    carry its own shorter 'ttl' (transient write-RPC backoff — retry within seconds, not 60s)."""
    rec = st.get("declined", {}).get(key)
    return bool(rec and (now_ts - rec["ts"]) < rec.get("ttl", DECLINE_TTL))


# --- pass / loop ---------------------------------------------------------------
def _seed_monitor_state() -> dict:
    ms = load_monitor_state()
    if CHECKPOINT_BLOCK is not None:
        try:
            ms["last_block"] = int(CHECKPOINT_BLOCK) - 1
        except ValueError:
            pass
    return ms


def once(st: dict | None = None, mstate: dict | None = None,
         skip_api: bool = False) -> tuple[int, int, list[dict]]:
    """One pass. Returns (n_targets HF<1, n_hot HF<HOT_HF, hot_rows) — hot_rows feed the
    predictive fast path (flip thresholds + arm candidates). skip_api=True re-reads the
    cached borrower set's HF on-chain without a Morpho-indexer call (hot-poll)."""
    own = st is None
    if own:
        st = load_state()
    now_ts = time.time()
    _roll_day(st, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    if skip_api:
        # hot tick: snappy client — retries=2 (the next 1s tick IS the retry), no pacing, no
        # long 429 backoff, and rotate the starting endpoint per pass so the 1Hz traffic isn't
        # concentrated on one public endpoint (review H10)
        rot = st["passes"] % len(DEFAULT_RPCS)
        rpc = Rpc(READ_RPCS or DEFAULT_RPCS[rot:] + DEFAULT_RPCS[:rot],
                  retries=2, min_interval=0.0, backoff_429=0.05)
    else:
        rpc = Rpc(READ_RPCS)
        refresh_eth_usd()
    if not DRY_RUN and st.get("sent"):
        _check_pending(st, now_ts)
    if mstate is None:
        mstate = _seed_monitor_state()

    r = scan(rpc, mstate, min_debt_usd=MIN_DEBT_USD, skip_api=skip_api)
    mstate.clear()
    mstate.update(r["state"])
    st["passes"] += 1

    # race telemetry (review M8): real Liquidate events (KT_LIQ_LOG_WINDOW>0) not sent by us =
    # a race we lost or never saw — THE metric hot-poll exists to move.
    if r["liquidations"]:
        if "last_liq_block" not in st:
            st["last_liq_block"] = r["block"]     # first sight: don't replay history
        ours = CONTRACT.lower()
        for lq in r["liquidations"]:
            if lq["block"] <= st["last_liq_block"]:
                continue
            if lq["liquidator"] != ours:
                msg = (f"🏁 RACE: {lq['borrower'][:10]}… liquidated by {lq['liquidator'][:10]}… "
                       f"repaid={lq['repaid_assets']} seized={lq['seized_assets']} "
                       f"blk={lq['block']}")
                print(f"  {msg}")
                alert(msg)
        st["last_liq_block"] = max(st["last_liq_block"],
                                   max(lq["block"] for lq in r["liquidations"]))

    ok, reason = guard_ok(st)
    print(f"[{time.strftime('%H:%M:%S')}] block {r['block']} | positions {r['n_positions']} | "
          f"near-edge {len(r['risk'])} | targets(HF<1) {len(r['targets'])} | "
          f"{'hot' if skip_api else 'API'} | guard={'OK' if ok else 'STOP('+reason+')'} "
          f"(DRY_RUN={'on' if DRY_RUN else 'OFF'}, contract={'set' if CONTRACT else 'none'})")
    if not ok:
        if own:
            save_state(st)
        raise GuardTripped(reason)

    gas_usd = gas_cost_usd(rpc)
    st.setdefault("declined", {})
    for t in sorted(r["targets"], key=lambda x: -(x["debt_usd"] or 0)):
        key = f"{t['market_id']}:{t['borrower']}"
        # dedup BEFORE the per-target RPC (_shares_for_repaid) and Sushi quotes, so a skipped
        # target costs nothing — critical at hot-poll cadence with perpetual bad-debt dregs
        if recently_fired(st, key, now_ts) or recently_declined(st, key, now_ts):
            continue
        # re-check the guards before EVERY fire, not once per pass — a cascade pass could
        # otherwise burn several reverts past the cap before the next pass notices
        ok, reason = guard_ok(st)
        if not ok:
            if own:
                save_state(st)
            raise GuardTripped(reason)
        # collateral-capped targets fire in seizedAssets mode — live shares are not needed
        t["borrow_shares_repaid"] = (0 if t["repaid_assets"] < t["debt_assets"]
                                     else _shares_for_repaid(rpc, t))
        ev = evaluate(rpc, t, gas_usd)
        if not ev:
            st["declined"][key] = {"ts": now_ts}
            print(f"  skip {t['borrower'][:10]}… HF={t['hf']:.4f}: no profitable chunk")
            continue
        nets = f"${ev['net_usd']:+,.1f}" if ev["net_usd"] is not None else f"{ev['net_wei']}wei"
        print(f"  target {t['borrower'][:10]}… HF={t['hf']:.4f} chunk={ev['f']:.0%} "
              f"net={nets} impact={ev['impact']*100:.2f}%")
        fire(rpc, t, ev, st, now_ts, gas_usd)
    # prune expired decline-cache entries so it can't grow unbounded (per-record ttl override)
    st["declined"] = {k: v for k, v in st["declined"].items()
                      if now_ts - v["ts"] < v.get("ttl", DECLINE_TTL)}
    if own:
        save_state(st)
    # imminent = any position (target or near-edge) within HOT_HF of the liquidation line -> hot-poll
    hot_rows = [x for x in r["targets"] + r["risk"] if x["hf"] < HOT_HF]
    return len(r["targets"]), len(hot_rows), hot_rows


_SEL_POSITION = selector("position(bytes32,address)")


def _shares_for_repaid(rpc: Rpc, t: dict) -> int:
    """Full-close repaidShares from the borrower's LIVE borrowShares, scaled to the capped repaid
    fraction of debt (the monitor caps repaid by collateral value / LIF). Read fresh so the tx
    uses current shares, not the possibly-stale scan snapshot. Shares scale linearly with assets."""
    pos = rpc.eth_call(
        MORPHO, _SEL_POSITION + t["market_id"][2:] + t["borrower"][2:].rjust(64, "0"))
    borrow_shares = int(pos[2 + 64:2 + 128], 16)   # word[1] = borrowShares
    if t["debt_assets"] <= 0:
        return 0
    # NOTE: collateral-capped closes never reach here — they fire in seizedAssets mode
    # (evaluate/liquidate_calldata), where Morpho derives repaid at execution price (M2).
    return borrow_shares * t["repaid_assets"] // t["debt_assets"]


def heartbeat(st: dict) -> None:
    if HEARTBEAT_SEC <= 0:
        return
    now_ts = time.time()
    if now_ts - st.get("last_heartbeat", 0) < HEARTBEAT_SEC:
        return
    st["last_heartbeat"] = now_ts
    alert(f"💓 katana executor alive: passes {st['passes']}, fires {st['fires']}, "
          f"reverts {st['reverts']}, races lost {st.get('races_lost', 0)}, "
          f"gas today ${st['gas_usd']:.2f}/${MAX_DAILY_GAS_USD}. "
          f"DRY_RUN={'on' if DRY_RUN else 'OFF'}.")


_last_balance_check = 0.0


def check_balance(st: dict, now_ts: float, force: bool = False) -> bool:
    """EOA gas-balance guard (there was NONE — a drained wallet only surfaced as an
    'insufficient funds' send-error storm mid-cascade). The node REJECTS a tx unless
    balance >= GAS_LIMIT*maxFeePerGas, i.e. the FULL fee envelope: with KT_FEE_BID that is
    GAS_LIMIT*(2*base + KT_MAX_PRIORITY_GWEI) ≈ 1.08 ETH at the 600 gwei cap (see STATE.md —
    funding '$50-100' was 10-40x short). Fire-readiness floor = max(one max-bid envelope,
    BALANCE_FIRES fires' burn at the default tip). Cheap: one eth_getBalance + one header per
    BALANCE_CHECK_SEC. Returns False (+ throttled TG alert) when underfunded."""
    global _last_balance_check
    if not force and now_ts - _last_balance_check < BALANCE_CHECK_SEC:
        return True
    addr = _owner_address()
    if not addr:
        return True
    _last_balance_check = now_ts
    try:
        bal = int(_rpc_write("eth_getBalance", [addr, "latest"]), 16)
        max_fee, cap_wei = _fee_params(MAX_PRIORITY_GWEI if FEE_BID else PRIORITY_GWEI)
        base = max(0, (max_fee - cap_wei) // 2)
        per_fire = GAS_UNITS_EST * (base + int(PRIORITY_GWEI * 1e9))
        need = max(GAS_LIMIT * max_fee, BALANCE_FIRES * per_fire)
    except Exception as e:
        print(f"balance check failed (skipped): {e}")
        return True
    st["balance_eth"] = round(bal / 1e18, 6)
    if bal >= need:
        return True
    msg = (f"⛽ LOW GAS BALANCE: {bal / 1e18:.4f} ETH < floor {need / 1e18:.4f} ETH "
           f"(node needs GAS_LIMIT×maxFee = {GAS_LIMIT * max_fee / 1e18:.4f} ETH per fire"
           f"{f' at the {MAX_PRIORITY_GWEI:.0f} gwei bid cap' if FEE_BID else ''}; "
           f"headroom K={BALANCE_FIRES} fires). Top up {addr}.")
    print(msg)
    if now_ts - st.get("last_balance_alert", 0) > BALANCE_ALERT_SEC:
        st["last_balance_alert"] = now_ts
        alert(msg)
    return False


def startup_preflight() -> None:
    """Fail LOUD at start instead of silently in the fire path (review C2): the live fire path
    imports eth_abi/eth_account lazily and DRY_RUN returns before reaching it, so a missing
    dep/binary/contract would only surface — swallowed as 'loop err' — on the first REAL
    target. Fatal problems alert + exit(1); soft ones print a warning."""
    probs, warns = [], []
    try:
        import eth_abi  # noqa: F401
    except ImportError:
        probs.append("python dep eth_abi missing (pip install -r requirements.txt)")
    if RAW_TX:
        try:
            import eth_account  # noqa: F401
        except ImportError:
            probs.append("python dep eth_account missing but KT_RAW_TX=1 "
                         "(pip install -r requirements.txt)")
    elif not shutil.which("cast"):
        probs.append("cast not on PATH but KT_RAW_TX=0")
    if not _tg_token() or not CHAT_ID:
        warns.append("telegram alerts DISABLED (set KT_TG_TOKEN or the channel env file, "
                     "and KT_CHAT_ID)")
    if not DRY_RUN:
        if not PRIVATE_KEY:
            probs.append("no private key (KT_PRIVATE_KEY / KT_KEYFILE)")
        elif RAW_TX and not _owner_address():
            probs.append("cannot derive an address from the private key")
        if not CONTRACT:
            probs.append("KT_CONTRACT is empty")
        else:
            try:
                cid = int(_rpc_write("eth_chainId", []), 16)   # also pre-warms the write conn
                if cid != CHAIN_ID:
                    probs.append(f"write RPC chainId {cid} != {CHAIN_ID}")
                if _rpc_write("eth_getCode", [_cs(CONTRACT), "latest"]) in ("0x", "", None):
                    probs.append(f"no code at KT_CONTRACT {CONTRACT}")
            except Exception as e:
                warns.append(f"write-RPC preflight failed (will retry in-loop): {e}")
        # EOA gas-balance guard (go-live precondition; re-checked periodically in the loop)
        if _owner_address():
            st = load_state()
            if not check_balance(st, time.time(), force=True):
                warns.append("EOA gas balance below the fire-readiness floor (TG alert sent)")
            save_state(st)
    for w in warns:
        print(f"preflight warning: {w}")
    if probs:
        msg = "🛑 katana executor preflight FAILED: " + "; ".join(probs)
        print(msg)
        alert(msg, sync=True)
        sys.exit(1)


def _kill(st: dict, g: Exception) -> None:
    msg = (f"🛑 KILL-SWITCH: {g}. Executor stopped — needs intervention "
           f"(python3 -m bot.executor reset, then restart).")
    print(msg)
    if time.time() - st.get("last_kill_alert", 0) > 900:   # cron restarts each minute
        st["last_kill_alert"] = time.time()
        alert(msg, sync=True)
    save_state(st)
    sys.exit(1)   # non-zero: a supervisor must see this as FAILURE, not exit 0 (C4)


def loop() -> None:
    startup_preflight()
    st = load_state()
    mstate = _seed_monitor_state()
    banner = (f"▶️ katana executor started (DRY_RUN={'on' if DRY_RUN else 'OFF'}, "
              f"min_profit ${MIN_PROFIT_USD}, contract={'set' if CONTRACT else 'NONE'}, "
              f"hot-poll {HOT_POLL_SEC}s<HF{HOT_HF}/API {API_REFRESH_SEC}s/idle {POLL_SEC}s, "
              f"predictive={'on' if PREDICTIVE_POLL else 'off'}, "
              f"kill-switch: gas ${MAX_DAILY_GAS_USD}/day, {MAX_CONSEC_REVERTS} reverts).")
    print(banner)
    # the cron watchdog resurrects this process every minute — throttle repeat banners so a
    # crash-loop doesn't turn Telegram into a firehose
    if time.time() - st.get("last_start_alert", 0) > 600:
        st["last_start_alert"] = time.time()
        save_state(st)
        alert(banner)
    st["last_heartbeat"] = time.time()
    last_api = 0.0
    n_hot = 0   # preserved across passes: an exception in once() must NOT drop a hot cascade
    #             to the idle POLL_SEC cadence — keep the last known hot count until a pass
    #             completes (resetting it per-iteration slept 20s with positions at the edge)
    hot_rows: list[dict] = []   # same rationale: keep the last known hot set on a bad pass
    clock = read_rpc = None
    if PREDICTIVE_POLL:
        # dedicated keep-alive read lane for the boundary tight-poll + armed price refresh:
        # snappy (no pacing, short timeout/429 backoff — the next block is the retry)
        read_rpc = Rpc(READ_RPCS or list(DEFAULT_RPCS), retries=2, min_interval=0.0,
                       backoff_429=0.05, timeout=3.0)
        clock = fastpath.BlockClock(read_rpc.poll_block_number)
    while True:
        try:
            # refresh the Morpho-indexer borrower set every API_REFRESH_SEC; between refreshes,
            # re-read the cached set's HF on-chain (skip_api) so we can hot-poll cheaply.
            do_api = (time.time() - last_api) >= API_REFRESH_SEC
            _, n_hot, hot_rows = once(st, mstate, skip_api=not do_api)
            if do_api:
                last_api = time.time()
        except GuardTripped as g:
            _kill(st, g)
        except Exception as e:
            print(f"loop err: {e}")
        heartbeat(st)
        if not DRY_RUN:
            check_balance(st, time.time())   # periodic EOA gas guard (throttled internally)
        save_state(st)
        if n_hot > 0 and clock is not None:
            # predictive mode: the block-boundary wait REPLACES the hot sleep — maintenance
            # runs in the idle zone, the armed window fires pre-signed on a flip, and the
            # next hot pass (immediately after) is the safety net / remainder-taker.
            try:
                if _predictive_cycle(read_rpc, clock, hot_rows, st):
                    continue
            except GuardTripped as g:
                _kill(st, g)
            except Exception as e:
                print(f"predictive err (falling back to hot cadence): {e}")
        elif n_hot > 0:
            _warm_write()   # classic hot cadence still keeps the fire lane warm
        # hot cadence when a position is within HOT_HF of liquidation, else idle cadence
        time.sleep(HOT_POLL_SEC if n_hot > 0 else POLL_SEC)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "once"
    if cmd == "once":
        # a manual `once` while the live loop runs must NOT fire real txs alongside it (no
        # flock on this path, shared nonce/state — review M7); explicit override to go live
        if not DRY_RUN and os.environ.get("KT_FORCE_LIVE_ONCE") != "1":
            globals()["DRY_RUN"] = True
            print("once: forcing DRY_RUN=1 (the live loop may be running; "
                  "set KT_FORCE_LIVE_ONCE=1 to really fire)")
        startup_preflight()
        try:
            once()
        except GuardTripped as g:
            print(f"KILL-SWITCH: {g}")
    elif cmd == "loop":
        loop()
    elif cmd == "reset":
        st = load_state()
        st["consec_reverts"] = 0
        st["gas_usd"] = 0.0
        st["sent"] = {}
        save_state(st)
        print("guard/dedup reset")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
