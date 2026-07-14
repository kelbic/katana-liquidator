"""Morpho GraphQL discovery — CURRENT borrowers near the liquidation edge, no historical scan.

Why: building the position book from getLogs(Borrow) block-0..head is impractical on a 37M-block
chain via a public RPC (rpc.katana.network truncates wide chunked responses; even chunked it is
very slow). The Morpho indexer already knows every open position, so we ask it directly for the
near-edge set (healthFactor <= ceiling) and then compute the EXACT trigger HF on-chain ourselves
via multicall (analysis.monitor.assess_candidates). API = discovery; chain = trigger.

READ-ONLY: HTTP POST GraphQL queries only. No keys, no transactions.

The API's healthFactor accrues interest (it is slightly LOWER than our stored-state on-chain HF,
which omits interest since lastUpdate — cross-checked: on-chain 1.048183 vs API 1.048075). So a
generous ceiling (default 1.15) cannot miss a position that is actually liquidatable now.
"""
from __future__ import annotations

import json
import urllib.request

API_URL = "https://api.morpho.org/graphql"
CHAIN_ID = 747474

_QUERY = """
query($chainId: Int!, $first: Int!, $skip: Int!, $hfMax: Float!) {
  marketPositions(
    first: $first, skip: $skip,
    where: { chainId_in: [$chainId], healthFactor_lte: $hfMax },
    orderBy: HealthFactor, orderDirection: Asc
  ) {
    items {
      healthFactor
      market { marketId collateralAsset { symbol } loanAsset { symbol } }
      user { address }
      state { borrowAssetsUsd borrowShares collateralUsd }
    }
  }
}
"""


def _post(query: str, variables: dict, timeout: float) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(API_URL, data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    if "errors" in d:
        raise RuntimeError(f"morpho graphql: {d['errors']}")
    return d["data"]


def fetch_candidates(chain_id: int = CHAIN_ID, hf_ceiling: float = 1.15,
                     market_ids: set[str] | None = None, min_debt_usd: float = 0.0,
                     page: int = 500, max_pages: int = 20, timeout: float = 25.0) -> dict:
    """Return {market_id(lower): set(borrower_lower)} — current borrowers with healthFactor <=
    hf_ceiling (i.e. the near-edge watch set). Optionally restrict to `market_ids` and/or a
    minimum on-indexer debt (borrowAssetsUsd). Paginates by `skip` until a short page or
    max_pages. Empty dict on any API failure is the caller's to handle (fall back to logs)."""
    want = {m.lower() for m in market_ids} if market_ids else None
    out: dict[str, set] = {}
    for i in range(max_pages):
        data = _post(_QUERY, {"chainId": chain_id, "first": page, "skip": i * page,
                              "hfMax": hf_ceiling}, timeout)
        items = data["marketPositions"]["items"]
        for it in items:
            st = it.get("state") or {}
            if (st.get("borrowShares") or 0) in (0, "0", None):
                continue
            if (st.get("borrowAssetsUsd") or 0) < min_debt_usd:
                continue
            mid = it["market"]["marketId"].lower()
            if want is not None and mid not in want:
                continue
            out.setdefault(mid, set()).add(it["user"]["address"].lower())
        if len(items) < page:
            break
    return out


def fetch_candidate_rows(chain_id: int = CHAIN_ID, hf_ceiling: float = 1.15,
                         page: int = 500, max_pages: int = 20, timeout: float = 25.0) -> list:
    """Same query but returns detailed rows (api_hf, debt_usd, symbols) for logging/diagnostics."""
    rows = []
    for i in range(max_pages):
        data = _post(_QUERY, {"chainId": chain_id, "first": page, "skip": i * page,
                              "hfMax": hf_ceiling}, timeout)
        items = data["marketPositions"]["items"]
        for it in items:
            st = it.get("state") or {}
            if (st.get("borrowShares") or 0) in (0, "0", None):
                continue
            m = it["market"]
            rows.append({
                "market_id": m["marketId"].lower(),
                "borrower": it["user"]["address"].lower(),
                "api_hf": it["healthFactor"],
                "debt_usd": st.get("borrowAssetsUsd") or 0.0,
                "pair": f"{(m.get('collateralAsset') or {}).get('symbol','?')}/"
                        f"{(m.get('loanAsset') or {}).get('symbol','?')}",
            })
        if len(items) < page:
            break
    return rows


if __name__ == "__main__":
    import sys
    ceil = float(sys.argv[1]) if len(sys.argv) > 1 else 1.15
    rows = fetch_candidate_rows(hf_ceiling=ceil)
    print(f"candidates with HF<={ceil}: {len(rows)}")
    for r in sorted(rows, key=lambda x: x["api_hf"])[:25]:
        print(f"  HF={r['api_hf']:.4f} debt=${r['debt_usd']:>12,.0f} {r['pair']:>16} "
              f"{r['borrower']} mkt={r['market_id'][:10]}")
