"""Sushi v7 swap API client for Katana (chainId 747474) — quotes AND swap calldata.

The Sushi RouteProcessor (0xAC4c…80b75) is the ONLY atomic exit for Katana vb-collateral
(vb tokens are 1:1 redeemable to L1 but the bridge round-trip is not atomic). The v7 API
returns both the expected output (for net/chunk sizing) and ready-to-execute tx.data whose
`to` is the RouteProcessor — exactly the (swapTarget, swapCallData) the KatanaLiquidator
callback needs. This mirrors how the Base reference bot consumes aggregator calldata.

READ-ONLY: this module only performs HTTP GET quotes. It never signs or sends a transaction.

Key integration nuance (drift safety): the RouteProcessor pulls `amountIn` (baked into the
calldata) from the caller. If the collateral actually seized on-chain is slightly LESS than
what we quoted (e.g. an adverse oracle tick between quote and execution reduces seizedAssets),
the swap would revert. We therefore quote for a slightly HAIRCUT amountIn (SWAP_INPUT_HAIRCUT),
so the baked amount is <= the real seized amount; any surplus collateral is dust the contract
sweeps. Quoting for less collateral is strictly safe (never over-pulls).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.sushi.com/swap/v7/747474"
ROUTE_PROCESSOR = "0xAC4c6e212A361c968F1725b4d055b47E63F80b75"

# Haircut applied to the swap input so the baked amountIn is <= the real seized collateral.
# 0.3% comfortably covers per-block oracle/interest drift on ~1s Katana blocks.
SWAP_INPUT_HAIRCUT = 0.003


class SushiError(RuntimeError):
    pass


class NoRouteError(SushiError):
    """No swappable route for this pair AT ANY SIZE (Sushi status 'NoWay', or a 4xx validation
    error for an unsupported/dust token). The caller should skip the target, not chunk down."""


def quote(token_in: str, token_out: str, amount_in_wei: int, sender: str, recipient: str,
          max_slippage: float = 0.005, timeout: float = 30.0, retries: int = 3) -> dict:
    """One Sushi v7 quote. Returns a normalised dict:
        {ok, amount_out (int), price_impact (float 0..1), gas (int),
         swap_target (str), swap_calldata (str 0x-hex), raw}
    `recipient` (to) is baked into the calldata — for the liquidation callback it MUST be the
    KatanaLiquidator contract so the swapped loanToken lands there for Morpho to pull.
    Raises NoRouteError (no retry) when there is no route / an unsupported token (status 'NoWay'
    or HTTP 4xx) — retrying those is pointless. Retries only transient network/5xx errors."""
    params = {
        "tokenIn": token_in, "tokenOut": token_out, "amount": str(int(amount_in_wei)),
        "maxSlippage": str(max_slippage), "sender": sender, "to": recipient,
    }
    url = API + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            if d.get("status") != "Success":
                # NoWay = no route at all; treat as fail-fast so the caller skips the target.
                if d.get("status") == "NoWay":
                    raise NoRouteError("no route (NoWay)")
                raise SushiError(f"status={d.get('status')} {str(d)[:160]}")
            tx = d.get("tx") or {}
            return {
                "ok": True,
                "amount_out": int(d["assumedAmountOut"]),
                "price_impact": float(d.get("priceImpact") or 0.0),
                "gas": int(d.get("gasSpent") or 0),
                "swap_target": tx.get("to"),
                "swap_calldata": tx.get("data"),
                "raw": d,
            }
        except SushiError:
            raise
        except urllib.error.HTTPError as e:
            # 429/408 are TRANSIENT (rate-limit/timeout — exactly what hot-poll cadence can
            # provoke), retry like a 5xx; only other 4xx mean the request/token is invalid.
            if e.code in (408, 429):
                last = e
                try:
                    ra = float(e.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    ra = 0.0
                time.sleep(min(max(ra, 0.5 * (attempt + 1)), 5.0))
            elif 400 <= e.code < 500:
                raise NoRouteError(f"HTTP {e.code} (unsupported token/amount)") from e
            else:
                last = e
                time.sleep(0.5 * (attempt + 1))
        except (OSError, ValueError) as e:
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise SushiError(f"quote failed after {retries}: {last}")


def quote_for_seized(coll_token: str, loan_token: str, seized_wei: int, recipient: str,
                     max_slippage: float = 0.005, haircut: float = SWAP_INPUT_HAIRCUT) -> dict:
    """Quote the exit for a liquidation that will seize `seized_wei` of collateral, applying the
    drift-safety haircut to the amountIn baked into the calldata. `recipient` = liquidator
    contract. `sender` is irrelevant to the calldata (only `to` is baked) so we pass recipient."""
    amount_in = int(seized_wei * (1.0 - haircut))
    q = quote(coll_token, loan_token, amount_in, sender=recipient, recipient=recipient,
              max_slippage=max_slippage)
    q["amount_in_used"] = amount_in
    q["haircut"] = haircut
    return q


def net_after_slippage(seized_wei: int, coll_decimals: int, coll_usd: float,
                       loan_decimals: int, loan_usd: float, lif: float,
                       coll_token: str, loan_token: str, recipient: str,
                       max_slippage: float = 0.02) -> dict:
    """Model the realised net of exiting `seized_wei` collateral: quote the swap, compute
    proceeds_usd, repaid_usd (= seized_value / lif), net = proceeds - repaid. Pure of gas
    (Katana gas ~ $0.005, added by the caller). Returns None-safe dict."""
    q = quote(coll_token, loan_token, seized_wei, sender=recipient, recipient=recipient,
              max_slippage=max_slippage)
    proceeds_usd = q["amount_out"] / 10 ** loan_decimals * loan_usd
    seized_usd = seized_wei / 10 ** coll_decimals * coll_usd
    repaid_usd = seized_usd / lif             # collateral seized = repaid * lif
    net_usd = proceeds_usd - repaid_usd
    return {
        "seized_usd": seized_usd, "proceeds_usd": proceeds_usd, "repaid_usd": repaid_usd,
        "net_usd": net_usd, "net_pct": (net_usd / repaid_usd if repaid_usd else 0.0),
        "price_impact": q["price_impact"], "amount_out": q["amount_out"],
    }
