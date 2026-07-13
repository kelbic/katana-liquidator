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
from analysis.models import lif_from_lltv, morpho_health_factor, shares_to_assets_up
from analysis.multicall import multicall
from analysis.protocols import (MORPHO, STABLES, TOKENS, TOPIC_MORPHO_BORROW,
                                TOPIC_MORPHO_LIQUIDATE, decode_morpho_borrow,
                                decode_morpho_liquidate)
from analysis.rpc import Rpc, get_logs_chunked

SEL_POSITION = selector("position(bytes32,address)")
SEL_MARKET = selector("market(bytes32)")
SEL_ID_TO_MARKET_PARAMS = selector("idToMarketParams(bytes32)")
SEL_PRICE = selector("price()")

# Katana Morpho was deployed well before we started watching; discovery walks from the first
# block we care about. 0 is safe (full history) but slow; the executor overrides with a recent
# checkpoint. Public Katana RPC accepts wide getLogs windows.
DEPLOY_BLOCK = int(os.environ.get("KT_DEPLOY_BLOCK", "0"))
LOG_CHUNK = int(os.environ.get("KT_LOG_CHUNK", "500000"))

WATCH_HF = 1.05         # positions below this form the watch set
REPORT_HF = 1.15        # risk table cutoff
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


def debt_usd(loan_addr: str, debt_assets: int) -> float | None:
    """USD size of debt. Stables (vbUSDC/vbUSDT) ~ $1 -> reliable; else None (sized in units)."""
    la = loan_addr.lower()
    if la in STABLES:
        return debt_assets / 10 ** _DEC.get(la, 6)
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
def scan(rpc: Rpc | None = None, state: dict | None = None,
         min_debt_usd: float = MIN_DEBT_USD, report_hf: float = REPORT_HF) -> dict:
    """One incremental pass. Returns:
        {block, targets:[...HF<1...], risk:[...HF<report_hf...], liquidations:[...since ckpt],
         state} — the executor uses `targets`; the CLI logs `risk` + `liquidations`.
    Each target/risk row carries the on-chain amounts needed to size a liquidation."""
    rpc = rpc or Rpc()
    state = state if state is not None else load_state()
    frm, to = state["last_block"] + 1, rpc.block_number()
    if to < frm:
        to = frm  # single-block pass

    # 1. discovery + real liquidations since checkpoint
    borrow_logs = get_logs_chunked(rpc, MORPHO, [TOPIC_MORPHO_BORROW], frm, to, chunk=LOG_CHUNK)
    pairs = {m: set(bs) for m, bs in state["pairs"].items()}
    for lg in borrow_logs:
        d = decode_morpho_borrow(lg)
        pairs.setdefault(d["market_id"], set()).add(d["borrower"])
    liq_logs = get_logs_chunked(rpc, MORPHO, [TOPIC_MORPHO_LIQUIDATE], frm, to, chunk=LOG_CHUNK)
    liquidations = [decode_morpho_liquidate(l) for l in liq_logs]

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

    flat = [(m, b) for m in mids for b in sorted(pairs[m])]
    res = multicall(rpc, [(MORPHO, SEL_POSITION + m[2:] + b[2:].rjust(64, "0"))
                          for m, b in flat])
    raw_pos = {}
    for (m, b), (ok, ret) in zip(flat, res):
        if ok and len(ret) >= 2 + 3 * 64:
            raw_pos[(m, b)] = decode_position(ret)
    for (m, b), p in list(raw_pos.items()):
        if p["borrow_shares"] == 0 and p["collateral"] == 0 and p["supply_shares"] == 0:
            pairs[m].discard(b)

    active = sorted({m for (m, b), p in raw_pos.items() if p["borrow_shares"] > 0})
    res = multicall(rpc, [(params[m]["oracle"], SEL_PRICE) for m in active])
    prices = {m: _words(ret)[0] for m, (ok, ret) in zip(active, res)
              if ok and len(ret) >= 66}

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

    new_state = {"last_block": to,
                 "pairs": {m: sorted(bs) for m, bs in pairs.items() if bs},
                 "params": params}
    return {"block": to, "from_block": frm, "targets": targets, "risk": risk,
            "liquidations": liquidations, "state": new_state, "n_positions": len(raw_pos)}


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
