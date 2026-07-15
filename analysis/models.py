"""Pure Gate-1 math — no network. Unit-tested in test_models.py.

Margin methodology (reused from the base/silo reference, rewritten for Monad):
  * Morpho: bonus = (LIF-1) * repaid_usd with LIF from lltv (protocol constants
    beta=0.3, maxLIF=1.15). bonus - gas = UPPER bound on winner net (ignores swap
    slippage & lost-race costs).
  * Euler V2: reverse dutch auction -> realized discount is NOT a protocol constant;
    measure it per event as value(seized) - value(repaid) from actual amounts.
"""
from __future__ import annotations

import datetime as _dt

WAD = 10 ** 18
_BETA = 0.3          # Morpho Blue LIQUIDATION_CURSOR
_MAX_LIF = 1.15      # Morpho Blue MAX_LIQUIDATION_INCENTIVE_FACTOR


def lif_from_lltv(lltv_wad: int) -> float:
    """Morpho Blue liquidation incentive factor: min(maxLIF, 1/(beta*lltv + (1-beta)))."""
    lltv = lltv_wad / WAD
    return min(_MAX_LIF, 1.0 / (_BETA * lltv + (1.0 - _BETA)))


def morpho_bonus_usd(repaid_usd: float, lltv_wad: int) -> float:
    """Upper-bound gross winner margin on a Morpho liquidation (before gas/slippage)."""
    return (lif_from_lltv(lltv_wad) - 1.0) * repaid_usd


def gross_margin_usd(seized_usd: float, repaid_usd: float) -> float:
    """Realized gross margin from actual amounts (Euler dutch / any protocol)."""
    return seized_usd - repaid_usd


def gas_cost_usd(gas_used: int, effective_gas_price_wei: int, native_usd: float) -> float:
    return gas_used * effective_gas_price_wei / 1e18 * native_usd


def top_share(counts: dict, n: int) -> float:
    """Share of the top-n keys in a {key: weight} mapping (0..1; 0 if empty)."""
    total = sum(counts.values())
    if total <= 0:
        return 0.0
    return sum(sorted(counts.values(), reverse=True)[:n]) / total


def hhi(counts: dict) -> float:
    """Herfindahl index of a {key: weight} mapping (0..1; 1 = monopoly)."""
    total = sum(counts.values())
    if total <= 0:
        return 0.0
    return sum((v / total) ** 2 for v in counts.values())


def month_key(ts: int) -> str:
    d = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
    return f"{d.year:04d}-{d.month:02d}"


def day_ts(ts: int) -> int:
    """Midnight UTC of the event's day — the key used for daily price lookups."""
    return ts - ts % 86400


# --- Morpho Blue position math (paper monitor; mirrors SharesMathLib/Morpho.sol) ----
VIRTUAL_SHARES = 10 ** 6
VIRTUAL_ASSETS = 1
ORACLE_PRICE_SCALE = 10 ** 36


def shares_to_assets_up(shares: int, total_assets: int, total_shares: int) -> int:
    """Morpho SharesMathLib.toAssetsUp — debt in assets from borrowShares (rounds up,
    against the borrower, exactly like the contract)."""
    num = shares * (total_assets + VIRTUAL_ASSETS)
    den = total_shares + VIRTUAL_SHARES
    return (num + den - 1) // den


def w_taylor_compounded(rate_wad: int, elapsed: int) -> int:
    """Morpho MathLib.wTaylorCompounded — 3-term Taylor of e^(rate*elapsed) - 1, WAD-scaled,
    rounding down each term exactly like the contract. `rate_wad` is the PER-SECOND borrow
    rate (WAD) returned by irm.borrowRateView."""
    first = rate_wad * elapsed
    second = first * first // (2 * WAD)
    third = second * first // (3 * WAD)
    return first + second + third


def accrued_interest(total_borrow_assets: int, borrow_rate_wad: int, elapsed: int) -> int:
    """Interest (loan assets) Morpho._accrueInterest would add to totalBorrowAssets after
    `elapsed` seconds since lastUpdate: totalBorrow.wMulDown(rate.wTaylorCompounded(elapsed)).
    liquidate() accrues BEFORE _isHealthy, so the real on-chain HF is computed against
    debt + this interest — stored-state HF alone is a stale upper bound."""
    if elapsed <= 0 or borrow_rate_wad <= 0:
        return 0
    return total_borrow_assets * w_taylor_compounded(borrow_rate_wad, elapsed) // WAD


def morpho_health_factor(collateral: int, oracle_price: int, lltv_wad: int,
                         borrowed_assets: int) -> float:
    """HF = maxBorrow / borrowed, maxBorrow = collateral * price / 1e36 * lltv / 1e18
    (Morpho.sol _isHealthy). float — paper precision; inf when no debt.
    NOTE: pass accrual-adjusted debt (see accrued_interest) — liquidate() accrues interest
    before _isHealthy, so stored-state HF alone is an overstated upper bound."""
    if borrowed_assets <= 0:
        return float("inf")
    max_borrow = collateral * oracle_price // ORACLE_PRICE_SCALE * lltv_wad // WAD
    return max_borrow / borrowed_assets


def per_month_usd(events: list[dict], usd_key: str, ts_key: str = "ts") -> dict:
    """{'YYYY-MM': summed usd} over events (skips events missing a usd value)."""
    out: dict = {}
    for e in events:
        v = e.get(usd_key)
        if v is None:
            continue
        k = month_key(e[ts_key])
        out[k] = out.get(k, 0.0) + v
    return out
