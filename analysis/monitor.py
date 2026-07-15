"""Katana Morpho position monitor — on-chain, READ-ONLY. Discovers borrowers, computes exact
HF from chain state (same math as Morpho.sol _isHealthy), flags liquidatable targets.

Used two ways:
  * CLI paper mode: `python3 -m analysis.monitor` — one pass, logs the risk table + WOULD-
    LIQUIDATE lines to data/monitor.log. No transactions, ever, on this path.
  * As the executor's target finder: `scan()` returns structured targets the live executor
    re-validates and fires on (bot/executor.py).

Each pass (cron-friendly single shot):
  1. Borrower discovery: incremental Morpho Borrow logs (owner = indexed onBehalf) since the
     checkpoint. Positions that read back fully empty are pruned (a later Borrow re-adds them).
  2. One Multicall3 sweep: idToMarketParams (immutable, cached) + market() state + position()
     for every tracked (market,borrower) + oracle.price() per active market. ~O(1) round-trips.
  3. Exact HF per position. Debt in loan assets is rounded UP (toAssetsUp, as the contract does).
  4. Target = HF < 1 with debt >= MIN_DEBT_USD. USD sizing uses the stable loan assumption
     (vbUSDC/vbUSDT ~ $1); non-stable loan markets are sized in loan units only.
State: data/monitor_state.json (atomic replace). Pure helpers unit-tested in test_monitor.py.
"""
from __future__ import annotations

import json
import os
import time

from analysis.keccak import selector
from analysis.models import (accrued_interest, lif_from_lltv, morpho_health_factor,
                             shares_to_assets_up)
from analysis.morpho_api import fetch_candidates
from analysis.multicall import MULTICALL3, multicall
from analysis.protocols import (MORPHO, STABLES, TOKENS, TOPIC_MORPHO_BORROW,
                                TOPIC_MORPHO_LIQUIDATE, decode_morpho_borrow,
                                decode_morpho_liquidate)
from analysis.rpc import Rpc, get_logs_chunked

SEL_POSITION = selector("position(bytes32,address)")
SEL_MARKET = selector("market(bytes32)")
SEL_ID_TO_MARKET_PARAMS = selector("idToMarketParams(bytes32)")
SEL_PRICE = selector("price()")
# IRM per-second borrow rate (WAD) for the accrual adjustment: liquidate() accrues interest
# BEFORE _isHealthy, so HF must be computed on debt + unaccrued interest, not stored totals —
# especially on quiet markets where lastUpdate can be days old (review C3).
SEL_BORROW_RATE_VIEW = selector(
    "borrowRateView((address,address,address,address,uint256),"
    "(uint128,uint128,uint128,uint128,uint128,uint128))")
SEL_MC3_TIMESTAMP = selector("getCurrentBlockTimestamp()")

# --- discovery -----------------------------------------------------------------
# Position book = CURRENT borrowers from the Morpho indexer (analysis.morpho_api), NOT a
# getLogs scan from block 0 (impractical on a 37M-block chain via the public RPC — it truncates
# wide chunked responses and is very slow). The API returns the near-edge set (HF <= ceiling)
# instantly; exact trigger HF is still computed on-chain here via multicall.
DISCOVERY = os.environ.get("KT_DISCOVERY", "api")     # "api" (default) | "logs"
API_HF_CEILING = float(os.environ.get("KT_API_HF_CEILING", "1.15"))
DEPLOY_BLOCK = int(os.environ.get("KT_DEPLOY_BLOCK", "0"))
LOG_CHUNK = int(os.environ.get("KT_LOG_CHUNK", "50000"))
# bounded window (in blocks back from head) for OPTIONAL supplements: fresh Borrows between API
# refreshes, and real Liquidate events for the CLI fidelity log. 0 = skip getLogs entirely.
INCR_WINDOW = int(os.environ.get("KT_INCR_WINDOW", "0"))
LIQ_LOG_WINDOW = int(os.environ.get("KT_LIQ_LOG_WINDOW", "0"))

WATCH_HF = 1.05         # positions below this form the watch set
REPORT_HF = 1.15        # risk table cutoff
# Hot-poll subset: positions within this HF of liquidation are re-swept on-chain on every hot pass
# (skip_api), so the hot loop re-reads only the imminent handful (~tens) instead of the whole book
# (~hundreds) — keeps a hot pass sub-second so we actually catch a cross within the hot cadence.
HOT_WATCH_HF = float(os.environ.get("KT_HOT_WATCH_HF", "1.05"))
MIN_DEBT_USD = float(os.environ.get("KT_MIN_DEBT_USD", "500"))

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
STATE_PATH = os.path.join(DATA_DIR, "monitor_state.json")
LOG_PATH = os.path.join(DATA_DIR, "monitor.log")

_DEC = {v["address"].lower(): v["decimals"] for v in TOKENS.values()}


# --- pure helpers (offline unit-tested) ---------------------------------------
def _words(h: str) -> list[int]:
    h = h[2:] if h.startswith("0x") else h
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def decode_position(ret_hex: str) -> dict:
    w = _words(ret_hex)
    return {"supply_shares": w[0], "borrow_shares": w[1], "collateral": w[2]}


def decode_market_state(ret_hex: str) -> dict:
    w = _words(ret_hex)
    return {"total_supply_assets": w[0], "total_supply_shares": w[1],
            "total_borrow_assets": w[2], "total_borrow_shares": w[3],
            "last_update": w[4], "fee": w[5]}


def decode_market_params(ret_hex: str) -> dict:
    w = _words(ret_hex)
    return {"loan": "0x" + hex(w[0])[2:].rjust(40, "0")[-40:],
            "collateral": "0x" + hex(w[1])[2:].rjust(40, "0")[-40:],
            "oracle": "0x" + hex(w[2])[2:].rjust(40, "0")[-40:],
            "irm": "0x" + hex(w[3])[2:].rjust(40, "0")[-40:],
            "lltv": w[4]}


def assess(pos: dict, mkt: dict, price: int, lltv: int) -> dict:
    """Exact debt (rounded up) + HF from chain state."""
    debt = shares_to_assets_up(pos["borrow_shares"], mkt["total_borrow_assets"],
                               mkt["total_borrow_shares"])
    hf = morpho_health_factor(pos["collateral"], price, lltv, debt)
    return {"debt_assets": debt, "collateral": pos["collateral"], "hf": hf}


def size_liquidation(debt_assets: int, collateral: int, price: int, lltv: int) -> dict:
    """Full-close sizing: repaidAssets capped by collateral value / LIF (can't seize more
    collateral than exists), seizedAssets = repaid * LIF / price. Mirrors Morpho.sol liquidate.
      seized = repaid * lif * 1e36 / price  (ORACLE_PRICE_SCALE = 1e36)
    Returns integer wei amounts for repaid (loan) and seized (collateral)."""
    lif = lif_from_lltv(lltv)
    coll_value_loan = collateral * price // 10 ** 36          # collateral worth, in loan units
    repaid = min(debt_assets, int(coll_value_loan / lif))
    seized = int(repaid * lif) * 10 ** 36 // price if price else 0
    seized = min(seized, collateral)
    return {"lif": lif, "repaid_assets": repaid, "seized_assets": seized}


def encode_borrow_rate_view(p: dict, mkt: dict) -> str:
    """Calldata for irm.borrowRateView(marketParams, market) — two static structs, 11 words."""
    words = [int(p["loan"], 16), int(p["collateral"], 16), int(p["oracle"], 16),
             int(p["irm"], 16), p["lltv"],
             mkt["total_supply_assets"], mkt["total_supply_shares"],
             mkt["total_borrow_assets"], mkt["total_borrow_shares"],
             mkt["last_update"], mkt["fee"]]
    return SEL_BORROW_RATE_VIEW + "".join(f"{w:064x}" for w in words)


# Coarse env prices for non-stable loan sizing (MIN_DEBT gate + target ordering ONLY — the
# profit gates in bot/executor.py use live Sushi quotes, never these).
ETH_USD_APPROX = float(os.environ.get("KT_ETH_USD", "3300"))
BTC_USD_APPROX = float(os.environ.get("KT_BTC_USD", "100000"))
_APPROX_USD = {TOKENS["vbETH"]["address"].lower(): ETH_USD_APPROX,
               TOKENS["weETH"]["address"].lower(): ETH_USD_APPROX,
               TOKENS["vbWBTC"]["address"].lower(): BTC_USD_APPROX,
               TOKENS["LBTC"]["address"].lower(): BTC_USD_APPROX}


def debt_usd(loan_addr: str, debt_assets: int) -> float | None:
    """USD size of debt. Stables (vbUSDC/vbUSDT) ~ $1 -> reliable; ETH/BTC-denominated loans
    use the coarse env price above (so vbETH-loan positions face the MIN_DEBT gate and sort by
    real size instead of bypassing both, review H1); unknown tokens -> None (still watched)."""
    la = loan_addr.lower()
    if la in STABLES:
        return debt_assets / 10 ** _DEC.get(la, 6)
    if la in _APPROX_USD:
        return debt_assets / 10 ** _DEC.get(la, 18) * _APPROX_USD[la]
    return None


# --- IO ------------------------------------------------------------------------
def load_state(path: str = STATE_PATH) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_block": DEPLOY_BLOCK - 1, "pairs": {}, "params": {}}


def save_state(state: dict, path: str = STATE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _bucket(hf: float) -> str:
    if hf < 1.0:
        return "LIQUIDATABLE"
    if hf < 1.02:
        return "CRITICAL"
    if hf < 1.05:
        return "RISKY"
    return "WATCH"


# --- core scan (shared by CLI + executor) --------------------------------------
def discover(rpc: Rpc, state: dict, to: int, hf_ceiling: float = API_HF_CEILING,
             market_ids: set[str] | None = None, skip_api: bool = False) -> tuple[dict, list]:
    """Build the candidate book {market_id: set(borrowers)}. Default mode "api": current
    near-edge borrowers from the Morpho indexer (instant, no historical scan). Mode "logs":
    incremental Borrow logs over a BOUNDED recent window only (never from block 0). Also returns
    real Liquidate events over an optional bounded window (CLI fidelity log; empty by default).
    On API failure, falls back to a bounded incremental-logs window so the pass still runs.

    skip_api=True (hot-poll): reuse the borrower book persisted from the last full discovery and do
    NO network discovery at all — the caller re-reads each position's HF on-chain via the multicall
    sweep in scan(). This lets the loop hot-poll HF every ~2s (cheap, on-chain) while refreshing the
    Morpho-indexer set only every ~30s (avoids hammering the public API), so we actually catch a
    position the instant it crosses HF<1 instead of ~20s late."""
    liquidations: list = []
    if skip_api:
        # hot-poll: re-sweep only the imminent subset (HF < HOT_WATCH_HF) cached by the last full
        # pass, so a hot pass stays sub-second. Fall back to the full book if no hot set yet.
        hot = state.get("hot_pairs") or state.get("pairs", {})
        return {m: set(bs) for m, bs in hot.items()}, liquidations

    if DISCOVERY == "api":
        # fresh near-edge set each pass — the indexer already tracks the current book, so we do
        # NOT accumulate stale (cured) borrowers across passes (keeps the multicall bounded).
        pairs: dict[str, set] = {}
        try:
            api = fetch_candidates(hf_ceiling=hf_ceiling, market_ids=market_ids)
            for m, bs in api.items():
                pairs.setdefault(m, set()).update(bs)
        except Exception as e:
            print(f"  API discovery failed ({e}); falling back to bounded incremental logs")
            pairs = {m: set(bs) for m, bs in state.get("pairs", {}).items()}
            _merge_incremental_logs(rpc, pairs, state, to, window=max(INCR_WINDOW, LOG_CHUNK))
    else:  # "logs": accumulate from the persisted book + a bounded incremental window (never 0)
        pairs = {m: set(bs) for m, bs in state.get("pairs", {}).items()}
        _merge_incremental_logs(rpc, pairs, state, to, window=INCR_WINDOW or LOG_CHUNK)

    # optional: fresh Borrows between API refreshes (bounded small window)
    if DISCOVERY == "api" and INCR_WINDOW > 0:
        _merge_incremental_logs(rpc, pairs, state, to, window=INCR_WINDOW)

    # optional: real liquidations for the CLI fidelity log (bounded window; off by default)
    if LIQ_LOG_WINDOW > 0:
        frm = max(0, to - LIQ_LOG_WINDOW)
        liq_logs = get_logs_chunked(rpc, MORPHO, [TOPIC_MORPHO_LIQUIDATE], frm, to, chunk=LOG_CHUNK)
        liquidations = [decode_morpho_liquidate(l) for l in liq_logs]
    return pairs, liquidations


def _merge_incremental_logs(rpc: Rpc, pairs: dict, state: dict, to: int, window: int) -> None:
    """Merge Borrow logs from a BOUNDED window (max(checkpoint, head-window)..head) into pairs."""
    frm = max(state.get("last_block", -1) + 1, to - window if window else to)
    if frm > to:
        return
    for lg in get_logs_chunked(rpc, MORPHO, [TOPIC_MORPHO_BORROW], frm, to, chunk=LOG_CHUNK):
        d = decode_morpho_borrow(lg)
        pairs.setdefault(d["market_id"], set()).add(d["borrower"])


def scan(rpc: Rpc | None = None, state: dict | None = None,
         min_debt_usd: float = MIN_DEBT_USD, report_hf: float = REPORT_HF,
         hf_ceiling: float = API_HF_CEILING, market_ids: set[str] | None = None,
         skip_api: bool = False) -> dict:
    """One pass. Returns:
        {block, targets:[...HF<1...], risk:[...HF<report_hf...], liquidations:[...],
         state} — the executor uses `targets`; the CLI logs `risk` + `liquidations`.
    Discovery is via the Morpho indexer by default (KT_DISCOVERY=api); exact trigger HF is
    computed on-chain here. Each target/risk row carries the on-chain amounts to size a liq."""
    rpc = rpc or Rpc()
    state = state if state is not None else load_state()
    to = rpc.block_number()

    # 1. discovery (Morpho indexer by default; bounded logs otherwise) + optional real liqs
    pairs, liquidations = discover(rpc, state, to, hf_ceiling=hf_ceiling, market_ids=market_ids,
                                   skip_api=skip_api)

    # 2. multicall sweep: params (new mids) -> market state -> positions -> prices
    params = dict(state["params"])
    new_mids = [m for m in pairs if m not in params]
    if new_mids:
        res = multicall(rpc, [(MORPHO, SEL_ID_TO_MARKET_PARAMS + m[2:]) for m in new_mids])
        for m, (ok, ret) in zip(new_mids, res):
            if ok and len(ret) >= 2 + 5 * 64:
                params[m] = decode_market_params(ret)
    mids = [m for m in pairs if m in params]
    res = multicall(rpc, [(MORPHO, SEL_MARKET + m[2:]) for m in mids])
    mstate = {m: decode_market_state(ret) for m, (ok, ret) in zip(mids, res)
              if ok and len(ret) >= 2 + 6 * 64}

    # positions + oracle prices + IRM rates + block timestamp in ONE aggregate3 round: fewer
    # round-trips AND internally consistent (single block) — position/price/rate can't come
    # from different replica heights (review M6/C3).
    flat = [(m, b) for m in mids for b in sorted(pairs[m])]
    live_mids = [m for m in mids if m in mstate]
    rate_mids = [m for m in live_mids if int(params[m]["irm"], 16) != 0]
    calls = ([(MORPHO, SEL_POSITION + m[2:] + b[2:].rjust(64, "0")) for m, b in flat]
             + [(params[m]["oracle"], SEL_PRICE) for m in live_mids]
             + [(params[m]["irm"], encode_borrow_rate_view(params[m], mstate[m]))
                for m in rate_mids]
             + [(MULTICALL3, SEL_MC3_TIMESTAMP)])
    res = multicall(rpc, calls)
    pos_res = res[:len(flat)]
    price_res = res[len(flat):len(flat) + len(live_mids)]
    rate_res = res[len(flat) + len(live_mids):len(flat) + len(live_mids) + len(rate_mids)]
    ts_ok, ts_ret = res[-1]
    chain_now = _words(ts_ret)[0] if ts_ok and len(ts_ret) >= 66 else int(time.time())

    raw_pos = {}
    for (m, b), (ok, ret) in zip(flat, pos_res):
        if ok and len(ret) >= 2 + 3 * 64:
            raw_pos[(m, b)] = decode_position(ret)
    for (m, b), p in list(raw_pos.items()):
        if p["borrow_shares"] == 0 and p["collateral"] == 0 and p["supply_shares"] == 0:
            pairs[m].discard(b)

    prices = {m: _words(ret)[0] for m, (ok, ret) in zip(live_mids, price_res)
              if ok and len(ret) >= 66}

    # accrue pending interest into the stored totals (what liquidate() itself does before
    # _isHealthy) — otherwise HF is systematically stale-high on quiet markets (review C3)
    for m, (ok, ret) in zip(rate_mids, rate_res):
        if not ok or len(ret) < 66:
            continue
        mkt = mstate[m]
        interest = accrued_interest(mkt["total_borrow_assets"], _words(ret)[0],
                                    chain_now - mkt["last_update"])
        if interest:
            mkt["total_borrow_assets"] += interest
            mkt["total_supply_assets"] += interest

    # 3. assess + build target/risk rows
    targets, risk = [], []
    for (m, b), p in raw_pos.items():
        if p["borrow_shares"] == 0 or m not in prices or m not in mstate:
            continue
        a = assess(p, mstate[m], prices[m], params[m]["lltv"])
        if a["hf"] >= report_hf:
            continue
        du = debt_usd(params[m]["loan"], a["debt_assets"])
        sz = size_liquidation(a["debt_assets"], p["collateral"], prices[m], params[m]["lltv"])
        row = {"market_id": m, "borrower": b, "hf": round(a["hf"], 6),
               "debt_assets": a["debt_assets"], "debt_usd": du,
               "collateral": p["collateral"], "price": prices[m],
               "loan": params[m]["loan"], "coll": params[m]["collateral"],
               "oracle": params[m]["oracle"], "irm": params[m]["irm"],
               "lltv": params[m]["lltv"], **sz}
        if a["hf"] < 1.0 and (du is None or du >= min_debt_usd):
            targets.append(row)
        else:
            risk.append(row)

    # Persist the params cache always (immutable, saves re-fetching idToMarketParams). Persist the
    # full pairs book so hot-poll passes can re-read HF between ~30s Morpho-indexer refreshes; and
    # persist the hot subset (imminent, HF<HOT_WATCH_HF) that a hot pass re-sweeps on-chain, so a hot
    # pass touches only the imminent handful and stays sub-second. On skip_api passes we only swept
    # the hot subset, so keep the last full book rather than shrinking it to the hot set.
    hot_pairs: dict = {}
    for row in targets + risk:
        if row["hf"] < HOT_WATCH_HF:
            hot_pairs.setdefault(row["market_id"], set()).add(row["borrower"])
    new_state = {"last_block": to, "params": params,
                 "pairs": ({m: sorted(bs) for m, bs in pairs.items() if bs}
                           if not skip_api else state.get("pairs", {})),
                 "hot_pairs": {m: sorted(bs) for m, bs in hot_pairs.items()}}
    return {"block": to, "from_block": state.get("last_block", -1) + 1, "targets": targets,
            "risk": risk, "liquidations": liquidations, "state": new_state,
            "n_positions": len(raw_pos)}


def run_once(rpc: Rpc | None = None, state_path: str = STATE_PATH,
             log_path: str = LOG_PATH) -> str:
    """CLI paper pass: scan, log the risk table + real liquidations, persist state. No tx."""
    state = load_state(state_path)
    r = scan(rpc, state)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lines = [f"=== {now} blocks {r['from_block']}..{r['block']} | positions {r['n_positions']} "
             f"| targets(HF<1) {len(r['targets'])} | risk(HF<{REPORT_HF}) {len(r['risk'])} "
             f"| real liqs since ckpt {len(r['liquidations'])}"]
    for t in sorted(r["targets"], key=lambda x: x["hf"]):
        du = f"${t['debt_usd']:,.0f}" if t["debt_usd"] is not None else "n/a"
        lines.append(f"  >>> WOULD LIQUIDATE hf={t['hf']:.4f} debt={du} "
                     f"repaid={t['repaid_assets']} seized={t['seized_assets']} "
                     f"mkt={t['market_id'][:10]} {t['borrower']}")
    for t in sorted(r["risk"], key=lambda x: x["hf"])[:15]:
        du = f"${t['debt_usd']:,.0f}" if t["debt_usd"] is not None else "n/a"
        lines.append(f"  hf={t['hf']:.4f} debt={du} mkt={t['market_id'][:10]} "
                     f"{t['borrower']} [{_bucket(t['hf'])}]")
    report = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a") as f:
        f.write(report)
    save_state(r["state"], state_path)
    return report


if __name__ == "__main__":
    print(run_once(), end="")
