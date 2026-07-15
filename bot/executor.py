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
  KT_HEARTBEAT_SEC (86400), KT_RAW_TX (0=cast [default], 1=in-process eth_account), DRY_RUN (1),
  KT_CHAT_ID / telegram env for alerts.

Usage:
    DRY_RUN=1 python3 -m bot.executor once     # single diagnostic pass (safe)
    DRY_RUN=0 KT_CONTRACT=0x.. python3 -m bot.executor loop   # live loop
    python3 -m bot.executor reset              # clear kill-switch / dedup
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from analysis.keccak import selector                                   # noqa: E402
from analysis.models import lif_from_lltv                              # noqa: E402
from analysis.monitor import scan, load_state as load_monitor_state    # noqa: E402
from analysis.protocols import MORPHO, STABLES, TOKENS                 # noqa: E402
from analysis.rpc import Rpc                                           # noqa: E402
from bot.sushi import quote, NoRouteError, SWAP_INPUT_HAIRCUT         # noqa: E402

# --- config -------------------------------------------------------------------
CONTRACT = os.environ.get("KT_CONTRACT", "")
PRIVATE_KEY = os.environ.get("KT_PRIVATE_KEY", "")
KEYFILE = os.path.expanduser(os.environ.get("KT_KEYFILE", "~/.katana-bot/key"))
if not PRIVATE_KEY and os.path.exists(KEYFILE):
    PRIVATE_KEY = open(KEYFILE).read().strip()
RPC_WRITE = os.environ.get("KT_RPC", "https://rpc.katana.network")
READ_RPCS = [os.environ["KT_READ_RPC"]] if os.environ.get("KT_READ_RPC") else None

MIN_PROFIT_USD = float(os.environ.get("KT_MIN_PROFIT_USD", "20"))
MIN_DEBT_USD = float(os.environ.get("KT_MIN_DEBT_USD", "500"))
MAX_SLIPPAGE = float(os.environ.get("KT_MAX_SLIPPAGE", "0.008"))   # swap floor sent to Sushi
MAX_IMPACT = float(os.environ.get("KT_MAX_IMPACT", "0.02"))        # chunk if impact above this
POLL_SEC = int(os.environ.get("KT_POLL_SEC", "20"))         # cadence when NOTHING is near-edge
# Hot-poll: when near-edge positions exist, re-read their HF on-chain this often (cheap; ~8ms RTT
# to the Katana RPC from our region) so we catch a cross within ~HOT_POLL_SEC instead of ~POLL_SEC.
# The Morpho-indexer set is refreshed only every API_REFRESH_SEC to avoid hammering the public API.
HOT_POLL_SEC = float(os.environ.get("KT_HOT_POLL_SEC", "1"))
API_REFRESH_SEC = float(os.environ.get("KT_API_REFRESH_SEC", "30"))
HOT_HF = float(os.environ.get("KT_HOT_HF", "1.02"))         # hot-poll only when a position is within
#                                                             this HF of liquidation (imminent cross)
GAS_LIMIT = int(os.environ.get("KT_GAS_LIMIT", "1800000"))        # generous (liq+swap+repay+sweep)
GAS_UNITS_EST = int(os.environ.get("KT_GAS_UNITS", "900000"))     # for gas-cost estimate
CHAIN_ID = int(os.environ.get("KT_CHAIN_ID", "747474"))
PRIORITY_GWEI = float(os.environ.get("KT_PRIORITY_GWEI", "0.001"))
MAX_DAILY_GAS_USD = float(os.environ.get("KT_MAX_DAILY_GAS_USD", "5"))
MAX_CONSEC_REVERTS = int(os.environ.get("KT_MAX_CONSEC_REVERTS", "3"))
DEDUP_SEC = int(os.environ.get("KT_DEDUP_SEC", "300"))
# Don't re-evaluate a target that was just declined ("no profitable chunk") — the perpetual
# bad-debt dregs (HF≈0.6-0.9, no profitable exit) sit near-edge forever, and at hot-poll cadence
# re-quoting them every pass would hammer the Sushi API (evaluate tries up to 8 chunk quotes each).
# A short TTL re-checks them occasionally without spamming; a freshly-crossed target is never in
# this cache so it's still evaluated instantly.
DECLINE_TTL = float(os.environ.get("KT_DECLINE_TTL", "60"))
HEARTBEAT_SEC = int(os.environ.get("KT_HEARTBEAT_SEC", "86400"))
RAW_TX = os.environ.get("KT_RAW_TX", "0") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"
CHECKPOINT_BLOCK = os.environ.get("KT_CHECKPOINT_BLOCK")
ETH_USD = float(os.environ.get("KT_ETH_USD", "3300"))            # gas USD estimate only

STATE_FILE = os.path.expanduser(os.environ.get("KT_STATE", "~/.katana-bot/exec_state.json"))
ENV_FILE = os.path.expanduser("~/.claude/channels/telegram/.env")
CHAT_ID = os.environ.get("KT_CHAT_ID", "265715923")

LIQUIDATE_SELECTOR = selector(
    "liquidate((address,address,address,address,uint256),address,uint256,address,bytes,uint256)")
_DEC = {v["address"].lower(): v["decimals"] for v in TOKENS.values()}
# chunk fractions tried, largest-first — the largest whose net clears the floor wins
CHUNK_FRACTIONS = (1.0, 0.75, 0.5, 0.35, 0.25, 0.15, 0.10, 0.06)


# --- state (kill-switch / dedup) ----------------------------------------------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"day": "", "gas_usd": 0.0, "consec_reverts": 0, "sent": {}, "declined": {},
            "last_heartbeat": 0, "passes": 0, "fires": 0, "reverts": 0}


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    json.dump(st, open(tmp, "w"))
    os.replace(tmp, STATE_FILE)


def _roll_day(st: dict, today: str) -> None:
    if st.get("day") != today:
        st["day"] = today
        st["gas_usd"] = 0.0


# --- telegram (optional; silent if not configured) ----------------------------
def alert(text: str) -> None:
    try:
        token = None
        with open(ENV_FILE) as f:
            for ln in f:
                if ln.startswith("TELEGRAM_BOT_TOKEN="):
                    token = ln.split("=", 1)[1].strip()
        if not token:
            return
        data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                   data=data), timeout=20)
    except Exception as e:
        print(f"alert fail: {e}")


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


# --- evaluate: size the exit against a live Sushi quote (chunking) ------------
def evaluate(rpc: Rpc, t: dict, gas_usd: float) -> dict | None:
    """Quote the exit for target `t` (a monitor scan row), chunking down until the net clears
    KT_MIN_PROFIT_USD. Returns fire params (repaidShares, swapTarget, swapCalldata, minProfit)
    or None if no chunk is profitable. loan/coll USD: stables ~ $1; collateral value via oracle."""
    loan, coll = t["loan"], t["coll"]
    loan_dec = token_decimals(rpc, loan)
    coll_dec = token_decimals(rpc, coll)
    lif = lif_from_lltv(t["lltv"])
    seized_full = t["seized_assets"]
    repaid_full_shares_units = t["repaid_assets"]   # loan assets to be repaid at full close
    if seized_full <= 0 or t["borrow_shares_repaid"] <= 0:
        return None
    loan_is_stable = loan.lower() in STABLES

    best = None
    for f in CHUNK_FRACTIONS:
        seized = int(seized_full * f)
        if seized <= 0:
            continue
        try:
            q = quote(coll, loan, int(seized * (1.0 - SWAP_INPUT_HAIRCUT)),
                      sender=CONTRACT or "0x000000000000000000000000000000000000dEaD",
                      recipient=CONTRACT or "0x000000000000000000000000000000000000dEaD",
                      max_slippage=MAX_SLIPPAGE)
        except NoRouteError:
            # no route at any size (dead/exotic collateral, e.g. yUSD) — skip this target
            return None
        except Exception as e:
            print(f"    quote fail f={f}: {e}")
            continue
        proceeds = q["amount_out"]
        # repaid for this chunk (loan wei) scales with the fraction of the full close
        repaid = int(repaid_full_shares_units * f)
        proceeds_usd = proceeds / 10 ** loan_dec * (1.0 if loan_is_stable else 0.0)
        repaid_usd = repaid / 10 ** loan_dec * (1.0 if loan_is_stable else 0.0)
        # net in loan wei (exact, currency-agnostic) and USD (stables only)
        net_wei = proceeds - repaid
        net_usd = (net_wei / 10 ** loan_dec) - gas_usd if loan_is_stable else None
        row = {"f": f, "seized": seized, "repaid_assets": repaid,
               "repaid_shares": int(t["borrow_shares_repaid"] * f),
               "proceeds": proceeds, "net_wei": net_wei, "net_usd": net_usd,
               "impact": q["price_impact"], "swap_target": q["swap_target"],
               "swap_calldata": q["swap_calldata"], "proceeds_usd": proceeds_usd,
               "repaid_usd": repaid_usd, "loan_dec": loan_dec}
        # profitability gate: stable loan -> USD net; non-stable -> require positive loan-wei net
        min_wei = int(MIN_PROFIT_USD * 10 ** loan_dec) if loan_is_stable else 1
        profitable = net_wei >= min_wei and q["price_impact"] <= MAX_IMPACT
        if loan_is_stable and net_usd is not None:
            profitable = profitable and net_usd >= MIN_PROFIT_USD
        if profitable:
            best = row
            break   # largest profitable chunk (fractions are descending)
    if best is None:
        return None
    # on-chain minProfit floor (2nd safety layer): USD floor in loan wei for stables, else a
    # conservative fraction of the quoted net for non-stable loans.
    if loan_is_stable:
        min_profit_wei = int(MIN_PROFIT_USD * 10 ** best["loan_dec"])
    else:
        min_profit_wei = max(1, best["net_wei"] // 2)
    best["min_profit_wei"] = min_profit_wei
    best["lif"] = lif
    return best


# --- calldata + signing --------------------------------------------------------
def liquidate_calldata(t: dict, ev: dict) -> str:
    from eth_abi import encode
    types = ["(address,address,address,address,uint256)", "address", "uint256",
             "address", "bytes", "uint256"]
    mp = (_cs(t["loan"]), _cs(t["coll"]), _cs(t["oracle"]), _cs(t["irm"]), t["lltv"])
    args = [mp, _cs(t["borrower"]), ev["repaid_shares"], _cs(ev["swap_target"]),
            bytes.fromhex(ev["swap_calldata"][2:]), ev["min_profit_wei"]]
    return LIQUIDATE_SELECTOR + encode(types, args).hex()


def _cs(addr: str) -> str:
    try:
        from eth_utils import to_checksum_address
        return to_checksum_address(addr)
    except Exception:
        return addr


# raw-tx write client (separate from the read-only Rpc whitelist)
def _rpc_write(method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC_WRITE, data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    if d.get("error"):
        raise RuntimeError(f"rpc {method}: {d['error']}")
    return d["result"]


def _fee_params() -> tuple[int, int]:
    priority = int(PRIORITY_GWEI * 1e9)
    try:
        blk = _rpc_write("eth_getBlockByNumber", ["latest", False])
        base = int(blk["baseFeePerGas"], 16)
        return base * 2 + priority, priority
    except Exception:
        gp = int(_rpc_write("eth_gasPrice", []), 16)
        return gp + priority, priority


def _fire_raw(t: dict, ev: dict, st: dict, now_ts: float, key: str, calldata: str) -> None:
    from eth_account import Account
    try:
        addr = Account.from_key(PRIVATE_KEY).address
        max_fee, priority = _fee_params()
        nonce = int(_rpc_write("eth_getTransactionCount", [addr, "pending"]), 16)
        tx = {"chainId": CHAIN_ID, "nonce": nonce, "to": _cs(CONTRACT), "value": 0,
              "gas": GAS_LIMIT, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority,
              "data": calldata}
        signed = Account.sign_transaction(tx, PRIVATE_KEY)     # key never logged
        raw = signed.raw_transaction
        raw_hex = raw.to_0x_hex() if hasattr(raw, "to_0x_hex") else "0x" + raw.hex()
        txh = _rpc_write("eth_sendRawTransaction", [raw_hex])
        rcpt, deadline = None, time.time() + 120
        while time.time() < deadline:
            rcpt = _rpc_write("eth_getTransactionReceipt", [txh])
            if rcpt:
                break
            time.sleep(2)
        if not rcpt:
            raise TimeoutError(f"no receipt in 120s: {txh}")
        status = "ok" if int(rcpt.get("status", "0x0"), 16) == 1 else "revert"
        _record(st, key, txh, now_ts, status)
        alert(f"{'✅ liq ok' if status == 'ok' else '❌ revert'}: {txh}")
    except Exception as e:
        _record(st, key, f"err:{e}", now_ts, "revert")
        alert(f"❌ raw-tx error: {e}")


def _fire_cast(t: dict, ev: dict, st: dict, now_ts: float, key: str, calldata: str) -> None:
    args = ["cast", "send", CONTRACT, calldata, "--gas-limit", str(GAS_LIMIT),
            "--rpc-url", RPC_WRITE, "--private-key", PRIVATE_KEY]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120)
        out = (r.stdout or "") + (r.stderr or "")
        reverted = ("status" in out and "0 (failed)" in out) or (r.returncode != 0)
        status = "revert" if reverted else "ok"
        _record(st, key, out[-80:].strip(), now_ts, status)
        alert(f"{'✅ liq ok' if status == 'ok' else '❌ revert'}: {out[-300:]}")
    except Exception as e:
        _record(st, key, f"err:{e}", now_ts, "revert")
        alert(f"❌ cast error: {e}")


def _record(st: dict, key: str, tx: str, now_ts: float, status: str) -> None:
    st["sent"][key] = {"tx": tx, "ts": now_ts, "status": status}
    if status == "revert":
        st["consec_reverts"] += 1
        st["reverts"] += 1
    else:
        st["consec_reverts"] = 0


def fire(t: dict, ev: dict, st: dict, now_ts: float, gas_usd: float) -> None:
    key = f"{t['market_id']}:{t['borrower']}"
    nets = f"${ev['net_usd']:+,.1f}" if ev["net_usd"] is not None else f"{ev['net_wei']} wei"
    if DRY_RUN or not CONTRACT:
        msg = (f"🧪 DRY_RUN: HF={t['hf']:.4f} chunk={ev['f']:.0%} repaidShares={ev['repaid_shares']} "
               f"net={nets} impact={ev['impact']*100:.2f}% mkt={t['market_id'][:10]} "
               f"{t['borrower'][:10]}…; NOT sent.")
        print(msg)
        return
    calldata = liquidate_calldata(t, ev)
    alert(f"🔫 LIQUIDATE HF={t['hf']:.4f} chunk={ev['f']:.0%} net~{nets} "
          f"impact={ev['impact']*100:.2f}% {t['borrower'][:10]}…, sending…")
    st["fires"] += 1
    st["gas_usd"] += gas_usd
    if RAW_TX:
        _fire_raw(t, ev, st, now_ts, key, calldata)
    else:
        _fire_cast(t, ev, st, now_ts, key, calldata)


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
    rec = st["sent"].get(key)
    return bool(rec and (now_ts - rec["ts"]) < DEDUP_SEC and rec.get("status") != "revert")


def recently_declined(st: dict, key: str, now_ts: float) -> bool:
    """True if this target was declined ('no profitable chunk') within DECLINE_TTL — skip re-quoting
    it (avoids hammering Sushi with the perpetual bad-debt dregs at hot-poll cadence)."""
    rec = st.get("declined", {}).get(key)
    return bool(rec and (now_ts - rec["ts"]) < DECLINE_TTL)


# --- pass / loop ---------------------------------------------------------------
def _seed_monitor_state() -> dict:
    ms = load_monitor_state()
    if CHECKPOINT_BLOCK is not None:
        try:
            ms["last_block"] = int(CHECKPOINT_BLOCK) - 1
        except ValueError:
            pass
    return ms


def once(st: dict | None = None, mstate: dict | None = None, skip_api: bool = False) -> tuple[int, int]:
    """One pass. Returns (n_targets HF<1, n_near_edge HF<report_hf). skip_api=True re-reads the
    cached borrower set's HF on-chain without a Morpho-indexer call (hot-poll)."""
    own = st is None
    if own:
        st = load_state()
    now_ts = time.time()
    _roll_day(st, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    rpc = Rpc(READ_RPCS)
    if mstate is None:
        mstate = _seed_monitor_state()

    r = scan(rpc, mstate, min_debt_usd=MIN_DEBT_USD, skip_api=skip_api)
    mstate.clear()
    mstate.update(r["state"])
    st["passes"] += 1

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
        t["borrow_shares_repaid"] = _shares_for_repaid(rpc, t)
        ev = evaluate(rpc, t, gas_usd)
        if not ev:
            st["declined"][key] = {"ts": now_ts}
            print(f"  skip {t['borrower'][:10]}… HF={t['hf']:.4f}: no profitable chunk")
            continue
        nets = f"${ev['net_usd']:+,.1f}" if ev["net_usd"] is not None else f"{ev['net_wei']}wei"
        print(f"  target {t['borrower'][:10]}… HF={t['hf']:.4f} chunk={ev['f']:.0%} "
              f"net={nets} impact={ev['impact']*100:.2f}%")
        fire(t, ev, st, now_ts, gas_usd)
    # prune expired decline-cache entries so it can't grow unbounded
    st["declined"] = {k: v for k, v in st["declined"].items() if now_ts - v["ts"] < DECLINE_TTL}
    if own:
        save_state(st)
    # imminent = any position (target or near-edge) within HOT_HF of the liquidation line -> hot-poll
    n_hot = sum(1 for x in r["targets"] + r["risk"] if x["hf"] < HOT_HF)
    return len(r["targets"]), n_hot


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
    return borrow_shares * t["repaid_assets"] // t["debt_assets"]


def heartbeat(st: dict) -> None:
    if HEARTBEAT_SEC <= 0:
        return
    now_ts = time.time()
    if now_ts - st.get("last_heartbeat", 0) < HEARTBEAT_SEC:
        return
    st["last_heartbeat"] = now_ts
    alert(f"💓 katana executor alive: passes {st['passes']}, fires {st['fires']}, "
          f"reverts {st['reverts']}, gas today ${st['gas_usd']:.2f}/${MAX_DAILY_GAS_USD}. "
          f"DRY_RUN={'on' if DRY_RUN else 'OFF'}.")


def loop() -> None:
    st = load_state()
    mstate = _seed_monitor_state()
    alert(f"▶️ katana executor started (DRY_RUN={'on' if DRY_RUN else 'OFF'}, "
          f"min_profit ${MIN_PROFIT_USD}, contract={'set' if CONTRACT else 'NONE'}, "
          f"hot-poll {HOT_POLL_SEC}s<HF{HOT_HF}/API {API_REFRESH_SEC}s/idle {POLL_SEC}s, "
          f"kill-switch: gas ${MAX_DAILY_GAS_USD}/day, {MAX_CONSEC_REVERTS} reverts).")
    st["last_heartbeat"] = time.time()
    last_api = 0.0
    while True:
        n_hot = 0
        try:
            # refresh the Morpho-indexer borrower set every API_REFRESH_SEC; between refreshes,
            # re-read the cached set's HF on-chain (skip_api) so we can hot-poll cheaply.
            do_api = (time.time() - last_api) >= API_REFRESH_SEC
            _, n_hot = once(st, mstate, skip_api=not do_api)
            if do_api:
                last_api = time.time()
        except GuardTripped as g:
            alert(f"🛑 KILL-SWITCH: {g}. Executor stopped — needs intervention "
                  f"(python3 -m bot.executor reset, then restart).")
            save_state(st)
            return
        except Exception as e:
            print(f"loop err: {e}")
        heartbeat(st)
        save_state(st)
        # hot cadence when a position is within HOT_HF of liquidation, else idle cadence
        time.sleep(HOT_POLL_SEC if n_hot > 0 else POLL_SEC)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "once"
    if cmd == "once":
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
