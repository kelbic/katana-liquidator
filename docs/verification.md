# Verification audit trail (2026-07-13)

Every number in STATE.md's verdict, with the exact source so it is reproducible. All data pulled
independently; on-chain addresses confirmed by code presence.

## Data sources
- **Morpho GraphQL** — `https://api.morpho.org/graphql` (markets, LLTV, oracles, positions/HF,
  liquidation history via `transactions(type_in:[MarketLiquidation])`).
- **Sushi v7 swap API** — `https://api.sushi.com/swap/v7/747474` (real slippage quotes + the
  RouteProcessor calldata the bot executes).
- **GeckoTerminal** — `https://api.geckoterminal.com/api/v2/networks/katana/pools` (pool depth).
- **Katana RPC** — `https://rpc.katana.network` (chainId, code presence, gas price, live HF check).

## On-chain confirmations
```
eth_chainId(rpc.katana.network)                       = 0xb67d2 = 747474            ✓
eth_getCode(0xD50F2Dff…Morpho)                         = 15,582 bytes                ✓
eth_getCode(0xAC4c6e21…Sushi RouteProcessor)           = 5,151 bytes                 ✓
eth_gasPrice                                           = 2,000,000 wei = 0.002 gwei  ✓ (~$0.005/liq @ 700k gas)
```

## Liquidation history (Morpho GraphQL, MarketLiquidation)
| Window | n | repaid $ | bonus $ (seized−repaid) | unique liquidators |
|--------|--:|--------:|-----------------------:|-------------------:|
| 30d | 30 | 338,985 | **24,170** | 12 |
| 90d | 94 | 1,240,185 | **85,724** | 21 |
| 180d | 269 | 2,203,614 | 166,537 | 30 |

Per-market (all 500 indexed events): vbWBTC/vbUSDC bonus $137.9k (165 liq, 13 liquidators);
vbETH/vbUSDC $66.6k (113, 16); vbWBTC/vbUSDT $33.8k (13, **4**); vbETH/vbUSDT $13.5k (28, 7);
LBTC/vbUSDC $9.7k (15, **3**). Largest single liquidation: **$233,776** repaid (vbWBTC/vbUSDC,
tx 0xf42780f7…), profitable at the historical BTC price (~$90.6k).

## Live markets (Morpho GraphQL, borrow > 0)
| market | borrow $ | LLTV | LIF bonus | marketId |
|--------|--------:|-----:|----------:|----------|
| weETH/vbETH | 13.6M | .915 | 2.62% | 0x1e74d36ffb… |
| vbWBTC/vbUSDC | 10.0M | .86 | 4.38% | 0xcd2dc555dc… |
| vbETH/vbUSDC | 2.5M | .86 | 4.38% | 0x2fb1471903… |
| vbWBTC/vbUSDT | 1.4M | .86 | 4.38% | 0xd4ab732112… |
| vbETH/vbUSDT | 704k | .86 | 4.38% | 0x9e03fc0dc3… |
| LBTC/vbUSDC | 423k | .86 | 4.38% | 0xa0cd6b9d1f… |

## Pool depth (GeckoTerminal, sushiswap-v3-katana)
vbUSDC/vbUSDT $2.95M · vbETH/weETH $1.67M · **vbWBTC/vbUSDC $1.16M** · vbUSDC/vbETH $637k ·
vbWBTC/LBTC $306k · BTCK/vbWBTC $300k · **vbWBTC/vbETH 0.3% only $25.7k**.
> Note: the brief's "vbWBTC/vbETH $565k" is not supported — GeckoTerminal shows ~$26k for that
> pool. It doesn't matter: the RouteProcessor uses the direct vbWBTC/vbUSDC pool ($1.16M) plus
> auto-splits, so the multi-hop-through-vbETH premise was moot.

## Real slippage model (Sushi v7, live) — vbWBTC → vbUSDC, LIF 4.38%
| exit $ | priceImpact | net bonus | net $ |
|-------:|-----------:|----------:|------:|
| 3,093 | 0.267% | +3.97% | +118 |
| 15,464 | 0.482% | +3.75% | +555 |
| 30,929 | 0.639% | +3.58% | +1,062 |
| 49,486 | 0.815% | +3.40% | +1,613 |
| 74,230 | 1.046% | +3.16% | +2,248 |
| 98,973 | 1.271% | +2.93% | +2,775 |
| 148,459 | 1.700% | +2.48% | +3,526 |
| 197,946 | 2.099% | +2.06% | +3,912 |

vbETH → vbUSDC (LIF 4.38%): +3.82% @ $1.8k → +3.13% @ $21k → +2.23% @ $97k → +1.72% @ $150k.

Reproduce: `python3 -c "from bot.sushi import quote; ..."` or the sweep in the git history of
this file's commit. Gas add-on ≈ $0.005 (negligible). No flash-loan fee (zero-capital callback).

## Live HF math cross-check
Real position (yvvbUSDC/vbUSDT, borrower 0x5C2A…b0b8, $112k debt): on-chain HF via
`analysis.monitor.assess` = **1.048183** vs Morpho API **1.048075** → Δ0.0001 = unaccrued
interest since `lastUpdate` (documented, expected direction). The monitor's HF is correct.
