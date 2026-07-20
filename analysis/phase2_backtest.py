"""Phase-2 funding gate: historical backtest of the Katana Morpho liquidator.

QUESTION: for each historical liquidation ticket we did NOT take (prize >= $300 in one of our
6 markets), would our bot have (a) SEEN the borrower and (b) ASSEMBLED a profitable exit route?

READ-ONLY: eth_call / eth_getBlockByNumber only. Nothing is signed, sent, or mutated. The
swap simulation runs entirely inside an eth_call `stateOverride` and is rolled back by a
reverting callback — it can never touch chain state.

--- METHOD (why this is a measurement and not a guess) --------------------------------------
The production evaluate() (bot/executor.py) prices the exit through the Sushi v7 HTTP API
(bot/sushi.py). That API has NO historical mode, so option (a) of the brief ("archival call to
the same quoter") is impossible AS WRITTEN — our quoter is off-chain.

However the Katana public RPC serves ARCHIVE state (verified: Morpho market() returns
different totals at old blocks) AND supports eth_call `stateOverride`. Every pool the Sushi
router actually routes our 6 pairs through is a Uniswap-V3-style pool (verified by extracting
pool addresses from the router's own tx.data and classifying them on-chain: all answer
slot0()/liquidity()/fee()). So we inject a minimal V3 path quoter (contracts-in-comment below,
runtime bytecode in QUOTER_RUNTIME) at a scratch address and run REAL V3 swap simulations
against the historical tick state at block-1.

Fidelity check against the live Sushi API at `latest` (see docs/phase2_backtest.md):
ratios 0.9992 - 1.0000 across all six markets, always <= 1.0. So this reconstruction is a
faithful, very slightly CONSERVATIVE stand-in for the production quoter. Where the router
would have split across pools it can only do better than our best-single-path number.

CAVEAT (stated in the report): we reconstruct the ROUTE and the ECONOMICS. We do NOT and
cannot reconstruct whether we would have WON the race — the observed winner tip is the price
of the ticket in OUR ABSENCE, not the price of displacing us. Everything here answers
"could we have participated", never "would we have won".
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.keccak import selector                                    # noqa: E402
from analysis.models import (accrued_interest, lif_from_lltv,           # noqa: E402
                             morpho_health_factor, shares_to_assets_up)
from analysis.multicall import MULTICALL3, decode_aggregate3, encode_aggregate3  # noqa: E402
from analysis.protocols import MARKETS, MORPHO, STABLES, TOKENS         # noqa: E402

# --- config ---------------------------------------------------------------------------------
RPCS = ["https://rpc.katanarpc.com", "https://rpc.katana.network"]
JOB = os.environ.get("KT_BT_JOB", "/home/claude-agent/.claude/jobs/d9c2c3f6/tmp")
OUT = os.environ.get("KT_BT_OUT", os.path.join(JOB, "phase2_backtest.json"))
QADDR = "0x00000000000000000000000000000000000A0001"   # scratch address for the injected quoter

# Production gates mirrored from bot/executor.py (defaults; env is NOT read so the backtest is
# reproducible regardless of the operator's shell).
MIN_PROFIT_USD = 20.0
MIN_DEBT_USD = 500.0
MAX_IMPACT = 0.02
GAS_UNITS_EST = 900_000
HOT_WATCH_HF = 1.05
REPORT_HF = 1.15
CHUNK_FRACTIONS = ((1, 1), (3, 4), (1, 2), (7, 20), (1, 4), (3, 20), (1, 10), (3, 50))
MIN_CHUNK_FRACTION = 0.0002
SWAP_INPUT_HAIRCUT = 0.003
_HAIRCUT_NUM = 1000 - int(round(SWAP_INPUT_HAIRCUT * 1000))
_HAIRCUT_DEN = 1000
# Phase-2 fee-bid parameters (bot/executor.py)
FEE_BID_MIN_NET_USD = 300.0
FEE_BID_KEEP_USD = 50.0
MAX_PRIORITY_GWEI = 600.0
PRIZE_FLOOR = 300.0

SEL_POSITION = selector("position(bytes32,address)")
SEL_MARKET = selector("market(bytes32)")
SEL_ID_TO_MARKET_PARAMS = selector("idToMarketParams(bytes32)")
SEL_PRICE = selector("price()")
SEL_BORROW_RATE_VIEW = selector(
    "borrowRateView((address,address,address,address,uint256),"
    "(uint128,uint128,uint128,uint128,uint128,uint128))")
SEL_MC3_TIMESTAMP = selector("getCurrentBlockTimestamp()")
SEL_QUOTE_PATH = selector("quotePath(address[],address,uint256)")

# Runtime bytecode of the read-only V3 path quoter (solc 0.8.24, optimizer 200). Source:
#   contract PathQuoter {
#     function quotePath(address[] pools, address tokenIn, uint256 amountIn) -> uint256 out
#       -- sequentially simulates pool.swap(); the reverting uniswapV3SwapCallback returns the
#          deltas, so nothing is ever settled. Returns 0 when any hop has no route/pool.
#     function uniswapV3SwapCallback(int256 a0, int256 a1, bytes) -> revert(abi(a0,a1))
#   }
_RT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phase2_quoter.hex")
QUOTER_RUNTIME = open(_RT_PATH).read().strip() if os.path.exists(_RT_PATH) else ""

# Sushi V3 pools the router actually uses for our pairs (extracted from RouteProcessor tx.data
# and verified on-chain: token0/token1/fee). Paths are collateral -> loan.
POOL = {
    "WBTC_USDC_3000": "0x4488005fd5eea2e22a80cb2a0e820ed6066e687f",
    "WBTC_USDC_500":  "0x744676b3ced942d78f9b8e9cd22246db5c32395c",
    "ETH_USDC_3000":  "0x105f833d8522f33d8dc3e9599455e9412b63d049",
    "ETH_USDC_100":   "0x136e82d68b61bcf76334c6227f53e43b8f79b3e8",
    "ETH_USDC_500":   "0x2a2c512beaa8eb15495726c235472d82effb7a6b",
    "USDC_USDT_100":  "0x5f88eeb3a5662489eb2d5da9f0c73f03355f3009",
    "LBTC_WBTC_500":  "0x0a2e4519ac308dddaa3c531f320b5d82e4fa84c3",
    "weETH_ETH_500":  "0xdfc0ba24be7f93bf1a9401635815ece4cc579282",
}
PATHS = {
    ("vbWBTC", "vbUSDC"): [[POOL["WBTC_USDC_3000"]], [POOL["WBTC_USDC_500"]]],
    ("vbETH", "vbUSDC"):  [[POOL["ETH_USDC_3000"]], [POOL["ETH_USDC_100"]],
                           [POOL["ETH_USDC_500"]]],
    ("vbWBTC", "vbUSDT"): [[POOL["WBTC_USDC_3000"], POOL["USDC_USDT_100"]],
                           [POOL["WBTC_USDC_500"], POOL["USDC_USDT_100"]]],
    ("vbETH", "vbUSDT"):  [[POOL["ETH_USDC_3000"], POOL["USDC_USDT_100"]],
                           [POOL["ETH_USDC_100"], POOL["USDC_USDT_100"]],
                           [POOL["ETH_USDC_500"], POOL["USDC_USDT_100"]]],
    ("LBTC", "vbUSDC"):   [[POOL["LBTC_WBTC_500"], POOL["WBTC_USDC_3000"]],
                           [POOL["LBTC_WBTC_500"], POOL["WBTC_USDC_500"]]],
    ("weETH", "vbETH"):   [[POOL["weETH_ETH_500"]]],
}
ETH_USD_PATHS = [[POOL["ETH_USDC_3000"]], [POOL["ETH_USDC_100"]], [POOL["ETH_USDC_500"]]]
SYM = {v["address"].lower(): k for k, v in TOKENS.items()}
DEC = {v["address"].lower(): v["decimals"] for v in TOKENS.values()}
MID_TO_NAME = {m["id"].lower(): n for n, m in MARKETS.items()}


# --- transport ------------------------------------------------------------------------------
class Rpc:
    """Tiny paced JSON-RPC client with rotation + 429 backoff (public endpoint is limited)."""

    def __init__(self, urls=None, timeout=60.0, retries=8, min_interval=0.10):
        self.urls = list(urls or RPCS)
        self.timeout, self.retries, self.min_interval = timeout, retries, min_interval
        self._last = 0.0

    def call(self, method: str, params: list):
        last = None
        for i in range(self.retries):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            url = self.urls[i % len(self.urls)]
            body = json.dumps({"jsonrpc": "2.0", "id": 1,
                               "method": method, "params": params}).encode()
            try:
                req = urllib.request.Request(
                    url, data=body,
                    headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = json.loads(r.read())
                if "error" in d:
                    raise RuntimeError(str(d["error"])[:200])
                return d["result"]
            except Exception as e:                                     # noqa: BLE001
                last = e
                time.sleep(min(0.6 * (i + 1), 4.0))
        raise RuntimeError(f"rpc exhausted: {last}")


def mc_at(rpc: Rpc, calls, block, overrides=None, gas=200_000_000):
    """Multicall3.aggregate3 at a historical block, optionally with a stateOverride."""
    tag = block if isinstance(block, str) else hex(block)
    data = encode_aggregate3(calls)
    params = [{"to": MULTICALL3, "data": data, "gas": hex(gas)}, tag]
    if overrides:
        params.append(overrides)
    return decode_aggregate3(rpc.call("eth_call", params))


def _words(h: str) -> list[int]:
    h = h[2:] if h.startswith("0x") else h
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def _addr(w: int) -> str:
    return "0x" + f"{w:040x}"[-40:]


# --- quoting --------------------------------------------------------------------------------
def enc_quote(pools, token_in, amount_in) -> str:
    head = ((0x60).to_bytes(32, "big") + int(token_in, 16).to_bytes(32, "big")
            + int(amount_in).to_bytes(32, "big"))
    arr = len(pools).to_bytes(32, "big") + b"".join(int(p, 16).to_bytes(32, "big")
                                                    for p in pools)
    return SEL_QUOTE_PATH + (head + arr).hex()


def quote_grid(rpc: Rpc, paths, token_in, amounts, block):
    """Quote EVERY (amount, path) pair at `block` in ONE aggregate3 round-trip.

    Returns [{amount, out, path, partial}] aligned to `amounts`, taking the best full-fill
    path per amount. `partial` marks a quote where the pool ran out of in-range liquidity and
    consumed less input than requested — the exact analogue of the Sushi router's 'Partial'
    status, which production evaluate() must never treat as a full fill (it would book
    proceeds for collateral the swap never actually took). Those rungs are rejected, and the
    ladder descends, precisely as bot/sushi.py PartialRouteError makes it descend live."""
    amounts = [int(a) for a in amounts]
    calls, index = [], []
    for ai, amt in enumerate(amounts):
        if amt <= 0:
            continue
        for p in paths:
            index.append((ai, p))
            calls.append((QADDR, enc_quote(p, token_in, amt)))
    out = [{"amount": a, "out": 0, "path": None, "partial": False} for a in amounts]
    if not calls:
        return out
    # A single V3 swap that exhausts in-range liquidity walks every initialised tick and can
    # burn millions of gas, and the node enforces its own eth_call gas ceiling — so batch
    # adaptively: split on "out of gas" down to single calls, and only then give up on that
    # rung (recorded as no-fill rather than silently dropping the whole grid).
    ov = {QADDR: {"code": QUOTER_RUNTIME}}
    pairs = []                       # [(index_entry, (ok, ret))]

    def _run(lo, hi):
        try:
            res = mc_at(rpc, calls[lo:hi], block, overrides=ov)
            pairs.extend(zip(index[lo:hi], res))
            return
        except Exception as e:                                         # noqa: BLE001
            if hi - lo <= 1 or "out of gas" not in str(e).lower():
                pairs.extend((index[i], (False, "0x")) for i in range(lo, hi))
                return
        mid = (lo + hi) // 2
        _run(lo, mid)
        _run(mid, hi)

    step = 6
    for s in range(0, len(calls), step):
        _run(s, min(s + step, len(calls)))
    for (ai, path), (ok, ret) in pairs:
        if not ok or len(ret) < 130:
            continue
        consumed, amt_out = _words(ret)[0], _words(ret)[1]
        # amt_out == 0 means no FULL fill at this size: consumed > 0 -> the router ran out of
        # acceptable depth (Sushi 'Partial'); consumed == 0 -> no pool/route at all.
        if amt_out <= 0:
            if consumed > 0:
                out[ai]["partial"] = True
            continue
        # allow 0.1% rounding tolerance before calling a fill "partial"
        if consumed < amounts[ai] * 999 // 1000:
            out[ai]["partial"] = True
            continue
        if amt_out > out[ai]["out"]:
            out[ai].update(out=amt_out, path=path)
    return out


def quote_best(rpc: Rpc, paths, token_in, amount_in, block):
    """Single-amount convenience wrapper over quote_grid (full fills only)."""
    r = quote_grid(rpc, paths, token_in, [amount_in], block)[0]
    return r["out"], r["path"]


# --- per-ticket replay ----------------------------------------------------------------------
def read_state(rpc: Rpc, mid: str, borrower: str, block: int):
    """Morpho state for (market, borrower) at `block`, accrued exactly as liquidate() does."""
    r1 = mc_at(rpc, [(MORPHO, SEL_ID_TO_MARKET_PARAMS + mid[2:]),
                     (MORPHO, SEL_MARKET + mid[2:]),
                     (MORPHO, SEL_POSITION + mid[2:] + borrower[2:].rjust(64, "0")),
                     (MULTICALL3, SEL_MC3_TIMESTAMP)], block)
    if not all(ok for ok, _ in r1[:3]):
        return None
    pw = _words(r1[0][1])
    params = {"loan": _addr(pw[0]), "collateral": _addr(pw[1]), "oracle": _addr(pw[2]),
              "irm": _addr(pw[3]), "lltv": pw[4]}
    mw = _words(r1[1][1])
    mkt = {"total_supply_assets": mw[0], "total_supply_shares": mw[1],
           "total_borrow_assets": mw[2], "total_borrow_shares": mw[3],
           "last_update": mw[4], "fee": mw[5]}
    pos_w = _words(r1[2][1])
    pos = {"supply_shares": pos_w[0], "borrow_shares": pos_w[1], "collateral": pos_w[2]}
    chain_now = _words(r1[3][1])[0] if r1[3][0] else 0

    rate_call = SEL_BORROW_RATE_VIEW + "".join(
        f"{w:064x}" for w in [int(params["loan"], 16), int(params["collateral"], 16),
                              int(params["oracle"], 16), int(params["irm"], 16), params["lltv"],
                              mkt["total_supply_assets"], mkt["total_supply_shares"],
                              mkt["total_borrow_assets"], mkt["total_borrow_shares"],
                              mkt["last_update"], mkt["fee"]])
    r2 = mc_at(rpc, [(params["oracle"], SEL_PRICE), (params["irm"], rate_call)], block)
    if not r2[0][0] or len(r2[0][1]) < 66:
        return None
    price = _words(r2[0][1])[0]
    if r2[1][0] and len(r2[1][1]) >= 66 and chain_now > mkt["last_update"]:
        interest = accrued_interest(mkt["total_borrow_assets"], _words(r2[1][1])[0],
                                    chain_now - mkt["last_update"])
        mkt["total_borrow_assets"] += interest
        mkt["total_supply_assets"] += interest
    return {"params": params, "mkt": mkt, "pos": pos, "price": price, "ts": chain_now}


def size_liquidation(debt_assets: int, collateral: int, price: int, lltv: int) -> dict:
    lif = lif_from_lltv(lltv)
    coll_value_loan = collateral * price // 10 ** 36
    repaid = min(debt_assets, int(coll_value_loan / lif))
    seized = int(repaid * lif) * 10 ** 36 // price if price else 0
    return {"lif": lif, "repaid_assets": repaid, "seized_assets": min(seized, collateral)}


def eth_usd_at(rpc: Rpc, block: int, cache: dict) -> float:
    """Spot ETH/USD at `block` from the vbETH->vbUSDC pools (1 vbETH in). 0 if unavailable."""
    key = block // 5000
    if key in cache:
        return cache[key]
    out, _ = quote_best(rpc, ETH_USD_PATHS, TOKENS["vbETH"]["address"], 10 ** 18, block)
    v = out / 10 ** 6 if out else 0.0
    cache[key] = v
    return v


# Counterfactual ladder: the production CHUNK_FRACTIONS floor is 3/50 (6%) of a FULL close.
# On whale positions a full close is orders of magnitude deeper than the pool, so even the
# smallest production rung is unfillable and evaluate() gives up. This geometric extension
# (down to 0.02% of a full close) measures how much of each prize a FINER SIZER could have
# reached. It is a DIAGNOSTIC, not a claim about the current bot.
DEEP_FRACTIONS = (0.06, 0.04, 0.025, 0.016, 0.010, 0.0063, 0.0040, 0.0025,
                  0.0016, 0.0010, 0.00063, 0.00040, 0.00025, 0.00016, 0.0001, 0.00005, 0.00002)


def _chunk_fractions(full_prize_usd, gas_usd):
    """Mirror of bot/executor.py _chunk_fractions: the static ladder, then a halving descent
    bounded below by f_min = (MIN_PROFIT_USD + gas_usd) / full_prize_usd (no fraction under it
    can clear the profit gate, since net(f) ~ f*full_prize - gas) and by MIN_CHUNK_FRACTION.
    Mirrored rather than imported for the same reason the gates above are: the backtest must
    reproduce byte-for-byte regardless of the operator's KT_* environment."""
    for num, den in CHUNK_FRACTIONS:
        yield num, den
    if not full_prize_usd or full_prize_usd <= 0:
        return
    f_lo = max((MIN_PROFIT_USD + gas_usd) / full_prize_usd, MIN_CHUNK_FRACTION)
    num, den = CHUNK_FRACTIONS[-1]
    while True:
        den *= 2
        if num / den < f_lo:
            return
        yield num, den


def _net_for(amount_in, proceeds, capped, seized_arg_scale, repaid_full, seized_full,
             price, lif, loan_dec, gas_usd):
    """USD net of exiting `amount_in` collateral for `proceeds` loan assets."""
    if capped:
        repaid = int(amount_in * price / 10 ** 36 / lif) + 1
    else:
        repaid = repaid_full * amount_in // max(1, seized_full * _HAIRCUT_NUM // _HAIRCUT_DEN)
    return (proceeds - repaid) / 10 ** loan_dec - gas_usd, repaid


def evaluate_hist(rpc: Rpc, row: dict, gas_usd: float, block: int) -> dict | None:
    """Historical twin of bot/executor.py evaluate(): the SAME chunk ladder, haircut, integer
    math and profit gates, with the Sushi HTTP quote replaced by an archival V3 path quote.
    Also runs the DEEP_FRACTIONS diagnostic ladder to measure the reachable net."""
    loan, coll = row["loan"].lower(), row["coll"].lower()
    paths = PATHS.get((SYM.get(coll), SYM.get(loan)))
    if not paths:
        return None
    loan_dec = DEC.get(loan, 6)
    loan_px = 1.0 if loan in STABLES else None
    if loan_px is None:
        return None                       # all 95 tickets are stable-loan; keep the gate honest
    lif = lif_from_lltv(row["lltv"])
    seized_full, repaid_full = row["seized_assets"], row["repaid_assets"]
    if seized_full <= 0:
        return None
    capped = repaid_full < row["debt_assets"]
    usd_floor_wei = max(1, int(MIN_PROFIT_USD / loan_px * 10 ** loan_dec))

    def amount_for(frac):
        seized = int(seized_full * frac)
        return max(0, seized * _HAIRCUT_NUM // _HAIRCUT_DEN)

    # production sizing = ladder + economically bounded descent (bot/executor.py). The prize is
    # (LIF-1)*repaid_full converted to USD, exactly as evaluate() computes full_prize_usd.
    full_prize_usd = (lif - 1.0) * repaid_full / 10 ** loan_dec * loan_px
    prod_fracs = [n / d for n, d in _chunk_fractions(full_prize_usd, gas_usd)]
    all_fracs = prod_fracs + [f for f in DEEP_FRACTIONS if f not in prod_fracs]
    amounts = [amount_for(f) for f in all_fracs]
    # Mid-price reference (~0.001% of a full close): evaluate() gets priceImpact from Sushi;
    # we derive it from our own quote curve against this near-zero-impact rung.
    ref_amount = max(1, amount_for(1.0) // 100_000)
    grid = quote_grid(rpc, paths, row["coll"], amounts + [ref_amount], block)
    ref = grid[-1]
    mid_px = (ref["out"] / ref["amount"]) if ref["out"] > 0 and ref["amount"] > 0 else None

    rows = []
    for f, g in zip(all_fracs, grid[:-1]):
        if g["out"] <= 0:
            rows.append({"f": f, "amount": g["amount"], "partial": g["partial"],
                         "net_usd": None, "impact": None})
            continue
        if capped:
            repaid = int(g["amount"] * row["price"] / 10 ** 36 / lif) + 1
        else:
            repaid = int(repaid_full * f)
        net_wei = g["out"] - repaid
        impact = (1 - (g["out"] / g["amount"]) / mid_px) if mid_px else None
        rows.append({"f": f, "amount": g["amount"], "partial": False,
                     "net_usd": net_wei / 10 ** loan_dec * loan_px - gas_usd,
                     "net_wei": net_wei, "proceeds": g["out"], "repaid": repaid,
                     "impact": impact, "path": g["path"]})

    by_f = {r["f"]: r for r in rows}
    # --- production result: largest ladder rung clearing the net floors AND MAX_IMPACT.
    # A rung over the impact cap does NOT end the search — evaluate() keeps descending, so
    # the ladder continues to smaller chunks exactly as it does live.
    prod = None
    for f in prod_fracs:
        r = by_f.get(f)
        if r and r["net_usd"] is not None and r["net_wei"] >= usd_floor_wei \
                and r["net_usd"] >= MIN_PROFIT_USD \
                and (r["impact"] is None or r["impact"] <= MAX_IMPACT):
            prod = r
            break
    # --- diagnostic: best net anywhere on the extended ladder ---
    elig = [r for r in rows if r["net_usd"] is not None
            and (r["impact"] is None or r["impact"] <= MAX_IMPACT)]
    deep = max(elig, key=lambda r: r["net_usd"], default=None)
    deep_out = None
    if deep and deep["net_usd"] >= MIN_PROFIT_USD and deep["net_wei"] >= usd_floor_wei:
        deep_out = {"f": deep["f"], "net_usd": deep["net_usd"],
                    "impact": deep["impact"]}

    if prod is not None:
        return {"f": prod["f"], "seized": int(seized_full * prod["f"]),
                "repaid_assets": prod["repaid"], "proceeds": prod["proceeds"],
                "net_wei": prod["net_wei"], "net_usd": prod["net_usd"], "path": prod["path"],
                "impact": prod["impact"], "lif": lif, "capped": capped,
                "deep": deep_out, "ladder": rows}
    return {"f": None, "fail": True, "deep": deep_out, "ladder": rows,
            "net_usd": max((r["net_usd"] for r in rows if r["net_usd"] is not None),
                           default=None),
            "all_partial": all(r["partial"] or r["net_usd"] is None for r in rows)}


def bid_cost_usd(net_usd: float, eth_usd: float) -> tuple[float, float]:
    """Phase-2 fee bid (gwei) and its USD cost, mirroring _competitive_priority_gwei()."""
    if net_usd is None or net_usd < FEE_BID_MIN_NET_USD or eth_usd <= 0:
        return 0.0, 0.0
    denom = GAS_UNITS_EST * eth_usd / 1e9
    affordable = (net_usd - FEE_BID_KEEP_USD) / denom if denom > 0 else 0.0
    gwei = min(MAX_PRIORITY_GWEI, max(affordable, 0.0))
    return gwei, gwei * denom


# --- driver ---------------------------------------------------------------------------------
def load_tickets():
    prized = json.load(open(os.path.join(JOB, "prized.json")))
    all_liqs = {l["tx"]: l for l in json.load(open(os.path.join(JOB, "all_liqs.json")))}
    tips = {}
    for t in json.load(open(os.path.join(JOB, "big_tips.json"))):
        tips.setdefault(t["blk"], []).append(t)
    out = []
    for p in prized:
        if not p["ours"] or p["prize"] < PRIZE_FLOOR:
            continue
        log = all_liqs.get(p["tx"])
        if not log:
            continue
        cand = tips.get(p["blk"]) or []
        tip = max((c["tip"] for c in cand), default=None)
        out.append({"blk": p["blk"], "mid": p["mid"], "tx": p["tx"], "prize": p["prize"],
                    "repaid_actual": p["repaid"], "winner_tip_gwei": tip,
                    "borrower": "0x" + log["topics"][3][-40:],
                    "market": MID_TO_NAME.get(p["mid"].lower(), p["mid"][:10])})
    return sorted(out, key=lambda r: r["blk"])


def run(limit=None, resume=True):
    rpc = Rpc()
    tickets = load_tickets()
    if limit:
        tickets = tickets[:limit]
    done = {}
    if resume and os.path.exists(OUT):
        done = {r["tx"]: r for r in json.load(open(OUT))}
    eth_cache, results = {}, []
    for i, t in enumerate(tickets):
        if t["tx"] in done:
            results.append(done[t["tx"]])
            continue
        blk = t["blk"] - 1                     # state BEFORE the competitor's liquidation
        rec = dict(t)
        try:
            blkdata = rpc.call("eth_getBlockByNumber", [hex(blk), False])
            rec["ts"] = int(blkdata["timestamp"], 16)
            base_fee = int(blkdata.get("baseFeePerGas", "0x0"), 16)
            st = read_state(rpc, t["mid"], t["borrower"], blk)
            if st is None:
                rec["error"] = "state read failed"
                results.append(rec); continue
            debt = shares_to_assets_up(st["pos"]["borrow_shares"],
                                       st["mkt"]["total_borrow_assets"],
                                       st["mkt"]["total_borrow_shares"])
            hf = morpho_health_factor(st["pos"]["collateral"], st["price"],
                                      st["params"]["lltv"], debt)
            loan = st["params"]["loan"].lower()
            du = (debt / 10 ** DEC.get(loan, 6)) if loan in STABLES else None
            rec.update({"hf": None if hf == float("inf") else round(hf, 6),
                        "debt_usd": du, "lltv": st["params"]["lltv"],
                        "collateral": st["pos"]["collateral"], "price": st["price"],
                        "loan": st["params"]["loan"], "coll": st["params"]["collateral"]})
            rec["visible_hf_lt_1"] = hf < 1.0
            rec["in_hot_set"] = hf < HOT_WATCH_HF
            rec["passes_min_debt"] = (du is None or du >= MIN_DEBT_USD)
            rec["is_target"] = bool(hf < 1.0 and rec["passes_min_debt"])

            eth_usd = eth_usd_at(rpc, blk, eth_cache)
            rec["eth_usd"] = round(eth_usd, 2)
            gas_usd = GAS_UNITS_EST * base_fee / 1e18 * eth_usd
            rec["gas_usd"] = round(gas_usd, 6)

            sz = size_liquidation(debt, st["pos"]["collateral"], st["price"],
                                  st["params"]["lltv"])
            row = {**rec, **sz, "debt_assets": debt}
            ev = evaluate_hist(rpc, row, gas_usd, blk)
            rec["seized_full"] = sz["seized_assets"]
            rec["repaid_full"] = sz["repaid_assets"]
            if ev is None:
                rec["route"] = "unsupported pair"
            elif ev.get("fail"):
                rec["route"] = "no profitable chunk"
                rec["best_net_usd"] = ev.get("net_usd")
                rec["all_partial"] = ev.get("all_partial")
                rec["deep"] = ev.get("deep")
            else:
                rec["route"] = "ok"
                rec["chunk_f"] = ev["f"]
                rec["net_usd"] = round(ev["net_usd"], 2)
                rec["capped"] = ev["capped"]
                gwei, cost = bid_cost_usd(ev["net_usd"], eth_usd)
                rec["bid_gwei"] = round(gwei, 2)
                rec["net_after_bid_usd"] = round(ev["net_usd"] - cost, 2)
                rec["passes_phase2_300"] = ev["net_usd"] >= FEE_BID_MIN_NET_USD
                rec["impact"] = ev.get("impact")
                rec["deep"] = ev.get("deep")
        except Exception as e:                                          # noqa: BLE001
            rec["error"] = repr(e)[:200]
        results.append(rec)
        print(f"[{i+1}/{len(tickets)}] blk={t['blk']} {t['market']:16s} "
              f"prize=${t['prize']:.0f} hf={rec.get('hf')} route={rec.get('route')} "
              f"net=${rec.get('net_usd', rec.get('best_net_usd'))}", flush=True)
        json.dump(results, open(OUT, "w"), indent=1)
    json.dump(results, open(OUT, "w"), indent=1)
    return results


if __name__ == "__main__":
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(limit=lim)
