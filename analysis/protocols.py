"""Katana protocol registry + pure Morpho log decoders (verified 2026-07-13).

Every address below was checked on-chain via Katana RPC (eth_getCode present) and/or the
Morpho GraphQL API (api.morpho.org). Topic0s are NEVER hand-pasted — computed offline via
analysis.keccak at import time (project rule: offline-keccak сверка).

Verification (see docs/verification.md):
  * Morpho Blue 0xD50F2Dff… — Morpho GraphQL morphoBlue.address for chain 747474 AND
    on-chain code (15582 bytes).
  * Sushi RouteProcessor 0xac4c6e21… — returned as tx.to by api.sushi.com/swap/v7/747474
    AND on-chain code (5151 bytes). This is the swap target for the callback.
"""
from __future__ import annotations

from analysis.keccak import event_topic0

CHAIN_ID = 747474  # Katana mainnet (0xb67d2)

# --- core protocol addresses (source 1: Morpho API / Sushi API; source 2: on-chain code) --
MORPHO = "0xD50F2DffFd62f94Ee4AEd9ca05C61d0753268aBc"          # Morpho Blue on Katana
SUSHI_ROUTE_PROCESSOR = "0xac4c6e212a361c968f1725b4d055b47e63f80b75"  # RP, swap target
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"       # canonical (same everywhere)

# --- token registry (Vault Bridge "vb" tokens; addresses from Morpho asset metadata) ------
# vb tokens are 1:1 redeemable to L1 via the Katana bridge, but redemption is NOT atomic
# (bridge round-trip). The ONLY atomic exit is Sushi. decimals matter for USD conversion.
TOKENS = {
    "vbWBTC": {"address": "0x0913DA6Da4b42f538B445599b46Bb4622342Cf52", "decimals": 8},
    "vbUSDC": {"address": "0x203A662b0BD271A6ed5a60EdFbd04bFce608FD36", "decimals": 6},
    "vbUSDT": {"address": "0x2DCa96907fde857dd3D816880A0df407eeB2D2F2", "decimals": 6},
    "vbETH":  {"address": "0xEE7D8BCFb72bC1880D0Cf19822eB0A2e6577aB62", "decimals": 18},
    "weETH":  {"address": "0x9893989433e7a383Cb313953e4c2365107dc19a7", "decimals": 18},
    "LBTC":   {"address": "0xecAc9C5F704e954931349Da37F60E39f515c11c1", "decimals": 8},
}
ADDR_TO_SYMBOL = {v["address"].lower(): k for k, v in TOKENS.items()}

# Stablecoin loan tokens (USD ≈ 1) — USD sizing of debt is reliable here.
STABLES = {TOKENS["vbUSDC"]["address"].lower(), TOKENS["vbUSDT"]["address"].lower()}

# --- target markets (full marketId from Morpho GraphQL; the liquidation opportunity set) --
# Ranked by realised 90d liquidation bonus + exit depth. lltv drives LIF (see models.py).
# The monitor ALSO auto-discovers every other market on-chain; this is just the priority set.
# All these markets share IRM 0x4F708C0ae7deD3d74736594C2109C2E3c065B428 (verified).
# Multiple markets exist per pair (older ones at $0 borrow are duplicates with dead oracles);
# these are the LIVE ones (non-trivial borrow as of 2026-07-13).
MARKETS = {
    "vbWBTC/vbUSDC": {  # $10.0M borrow — deepest exit ($1.16M direct pool + splits)
        "id": "0xcd2dc555dced7422a3144a4126286675449019366f83e9717be7c2deb3daae3e",
        "lltv": 0.86, "loan": "vbUSDC", "coll": "vbWBTC",
        "oracle": "0xB60F728BdcE5e3921C0E42c1a6F07A1313D0040e"},
    "vbETH/vbUSDC": {   # $2.5M borrow
        "id": "0x2fb14719030835b8e0a39a1461b384ad6a9c8392550197a7c857cf9fcbd6c534",
        "lltv": 0.86, "loan": "vbUSDC", "coll": "vbETH",
        "oracle": "0xD423D353f890aD0D18532fFaf5c47B0Cb943bf47"},
    "vbWBTC/vbUSDT": {  # $1.4M borrow — only 4 unique liquidators (thin competition)
        "id": "0xd4ab732112fa9087c9c3c3566cd25bc78ee7be4f1b8bdfe20d6328debb818656",
        "lltv": 0.86, "loan": "vbUSDT", "coll": "vbWBTC",
        "oracle": "0x07A9c82f38aAD9855FaF76D398F9C64a7A12F0AE"},
    "vbETH/vbUSDT": {   # $704k borrow — 7 unique liquidators
        "id": "0x9e03fc0dc3110daf28bc6bd23b32cb20b150a6da151856ead9540d491069db1c",
        "lltv": 0.86, "loan": "vbUSDT", "coll": "vbETH",
        "oracle": "0x2477367cFF71b31b4BE6963e5691859E8fcDF084"},
    "LBTC/vbUSDC": {    # $423k borrow — only 3 unique liquidators
        "id": "0xa0cd6b9d1fcc6baded4f7f8f93697dbe7f24f6e1fc22602a625c7a80b8e8e6ef",
        "lltv": 0.86, "loan": "vbUSDC", "coll": "LBTC",
        "oracle": "0xcC139318686969b9D30Dd62aA206725B269DA40d"},
    "weETH/vbETH": {    # $13.6M borrow — biggest, but correlated pair, thin LIF (2.6%)
        "id": "0x1e74d36ffbda65b8a45d72754b349cdd5ce807c5fa814f91ba8e3cd27881c34b",
        "lltv": 0.915, "loan": "vbETH", "coll": "weETH",
        "oracle": "0xD0457014ae86DF159482Ad6ddaD9bB6827DF4bc9"},
}

# --- Morpho event signatures (canonical) --------------------------------------------------
SIG_MORPHO_LIQUIDATE = "Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
SIG_MORPHO_CREATE_MARKET = "CreateMarket(bytes32,(address,address,address,address,uint256))"
SIG_MORPHO_BORROW = "Borrow(bytes32,address,address,address,uint256,uint256)"

TOPIC_MORPHO_LIQUIDATE = event_topic0(SIG_MORPHO_LIQUIDATE)
TOPIC_MORPHO_CREATE_MARKET = event_topic0(SIG_MORPHO_CREATE_MARKET)
TOPIC_MORPHO_BORROW = event_topic0(SIG_MORPHO_BORROW)


# --- pure decoders (unit-tested offline) --------------------------------------------------
def _addr(word_hex: str) -> str:
    return "0x" + word_hex[-40:].lower()


def _words(data: str) -> list[int]:
    h = data[2:] if data.startswith("0x") else data
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def decode_morpho_liquidate(log: dict) -> dict:
    """Morpho Blue Liquidate(id, caller, borrower, repaidAssets, repaidShares,
    seizedAssets, badDebtAssets, badDebtShares). caller/borrower are indexed topics."""
    w = _words(log["data"])
    return {
        "protocol": "morpho",
        "block": int(log["blockNumber"], 16),
        "tx": log["transactionHash"],
        "market_id": log["topics"][1].lower(),
        "liquidator": _addr(log["topics"][2]),
        "borrower": _addr(log["topics"][3]),
        "repaid_assets": w[0],
        "repaid_shares": w[1],
        "seized_assets": w[2],
        "bad_debt_assets": w[3],
    }


def decode_morpho_borrow(log: dict) -> dict:
    """Morpho Blue Borrow(id, caller, onBehalf, receiver, assets, shares).
    Position owner = indexed onBehalf (topics[2])."""
    return {
        "protocol": "morpho",
        "block": int(log["blockNumber"], 16),
        "tx": log["transactionHash"],
        "market_id": log["topics"][1].lower(),
        "borrower": _addr(log["topics"][2]),
    }
