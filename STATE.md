# STATE — Katana Morpho Liquidator

Battle-ready liquidation bot for Morpho Blue on **Katana** (chainId 747474). This file is the
verdict + architecture + economics + decision log. Operator handoff steps are in `README.md`.

Status as of **2026-07-13**: verification done, code built, **fork-tested end-to-end**, deploy-
ready. Stopped BEFORE live mainnet deploy (operator's key + funding = operator's step).

**Update 2026-07-15 — LIVE + hot-poll.** Deployed live (DRY_RUN=0). Diagnosed why it had 0 fires:
NOT sizing/markets (measured — all ~$4.2M/500-liq flow is in the 6 registered markets; the
persistent "no profitable chunk" declines are correct bad-debt dregs, coll≪debt). It was the flat
20s poll on a ~1s-block chain — fresh liquidations were taken by fast bots seconds before our next
look, so we only ever saw the dregs. We're ~8ms from the Katana RPC (EU), so latency is NOT the
constraint. Fix (commit b1d8108): hot-poll the imminent subset (HF<HOT_WATCH_HF) on-chain every
~1s (hot pass ~1.3s) with a full Morpho-indexer refresh every ~30s; decline-dedup so bad-debt
dregs don't re-hammer Sushi; multicall chunk 100→250. Firing logic unchanged. Effective look-
cadence ~20s → ~2s. Truly winning 1-block races would need event-driven detection (future v2).

---

## ⭐ TASK 1 — VERIFICATION VERDICT: **+EV is REAL** (and bigger than the initial estimate)

Everything below was pulled independently (Morpho GraphQL, GeckoTerminal, Sushi v7 API, Katana
RPC) and cross-checked. Where my numbers differ from the mission brief, mine are cited.

### The opportunity is real and larger than the brief said
| Window | Liquidations | Debt repaid | **Bonus (LIF surplus)** | Unique liquidators |
|--------|-------------:|------------:|------------------------:|-------------------:|
| last 30d | 30 | $338,985 | **$24,170** | 12 |
| last 90d | 94 | $1,240,185 | **$85,724** | 21 |
| last 180d | 269 | $2,203,614 | $166,537 | 30 |

- Realised bonus is **~$24–28k / month**, not the ~$11k/mo in the brief. The market is ~2.5×
  bigger than estimated. "21 unique liquidators / 90d" in the brief is confirmed exactly.
- Bonus concentrates in **vbWBTC/vbUSDC** ($137.9k all-time, 13 liquidators) and
  **vbETH/vbUSDC** ($66.6k, 16 liquidators). Not monopolised. Tail markets are thinner still:
  **vbWBTC/vbUSDT** (4 liquidators), **vbETH/vbUSDT** (7), **LBTC/vbUSDC** (3) — the edge.

### The slippage fear is REFUTED by real quotes
The brief worried a $146k liquidation = ~13% of the vbWBTC pool → slippage eats the 4.5% LIF.
Real Sushi v7 quotes (RouteProcessor auto-splits across the direct pool **and** the
vbWBTC→vbETH→vbUSDC multi-hop) say otherwise:

**vbWBTC → vbUSDC, net after slippage (LIF 4.38%, gas ≈ $0.005, zero flash-loan):**
| Exit size | price impact | **net bonus** | net $ |
|----------:|-------------:|--------------:|------:|
| $3k   | 0.27% | **+3.97%** | +$118 |
| $31k  | 0.64% | **+3.58%** | +$1,062 |
| $49k  | 0.82% | **+3.40%** | +$1,613 |
| $99k  | 1.27% | **+2.93%** | +$2,775 |
| $148k | 1.70% | **+2.48%** | +$3,526 |
| $198k | 2.10% | **+2.06%** | +$3,912 |

A **$148k single-shot exit still nets +2.48% (+$3,526)** — slippage does *not* eat the bonus.
vbETH/vbUSDC is similar (+3.8% at $2k → +1.7% at $150k). The pool is deeper than raw TVL
implies because it is V3 concentrated liquidity + the router splits routes.

### Where the +EV lives
- **Every realistic chunk size is +EV.** Sweet spot for capital efficiency: **$30k–$100k
  chunks net 2.9–3.6%.** Above ~$150k, chunk it (the bot does this automatically).
- **Zero standing capital, no flash loan.** Morpho seizes collateral to the contract first; the
  callback swaps it to repay. Gas on Katana is ~**$0.005** per liquidation (0.002 gwee). So net
  ≈ gross LIF minus slippage, full stop.
- **Fork-proven end-to-end** (see Task 2): real Morpho liquidate + real Sushi swap both execute.

### Honest risks
1. **Bursty flow.** Most days nothing; liquidations cluster on volatility events. The $24–28k/mo
   is lumpy. Realistic *capturable* net after competition ≈ **20–40% of gross → $5–11k/mo**,
   concentrated in a few events. Position sizing/latency matters less here than on contested
   chains (tail markets, fee-auction races, not top-of-block FCFS — same finding as the Base
   reference), so a correct, always-on bot with good routing captures a fair share.
2. **Single-DEX exit.** vb tokens are 1:1 redeemable to L1 but the bridge round-trip is **not
   atomic** — the ONLY atomic exit is Sushi. If Sushi liquidity recedes (it is partly
   incentive-injected "Chain-Owned Liquidity"), slippage rises. The bot's chunking + on-chain
   minProfit gate mean the downside is "skip / take a smaller chunk", never a loss.
3. **weETH/vbETH is a trap for the naive.** It is the biggest market ($13.6M borrow) and right
   now has a cluster of positions sitting at **HF ≈ 1.00–1.02** ($2.5M, $3.1M, $1.5M…). But it
   is a *correlated* pair (weETH vs ETH), LIF is only **2.6%**, and the weETH→vbETH exit pool is
   ~$1.67M. Big notional, thin margin, thin exit. The bot watches it but sizes conservatively.
4. **Competition will intensify** as the chain grows — which is exactly why being in line early
   matters (below).

### Option / growth value (per operator's addendum)
Beyond today's spot economics, Katana is a **cheap early option on a growing chain**:
- Build-and-hold cost is ~zero: one ~$0.01 deploy, a hot wallet with a few dollars of gas, a
  cron/systemd process. No standing capital at risk (zero-capital callback + DRY_RUN default).
- Being **in the liquidation line early — while there are only 4–16 competitors and before pro
  MEV desks arrive — is itself the edge.** Flow and TVL on Katana are trending up; today's thin,
  lumpy stream is the *entry price* for a lane that compounds if the chain scales. "Collect while
  they give, stay mobile."
- The asymmetry is right: bounded, near-zero cost to hold the position; open-ended upside if
  Katana grows; the kill-switch + minProfit gate cap the downside of any single action.

**Verdict: build and deploy.** +EV today ($5–11k/mo capturable, net-positive at every realistic
size), near-zero carrying cost, and a real early-mover option on chain growth. The one thing NOT
to do is oversize into weETH/vbETH.

---

## ⭐ TASK 2 — ARCHITECTURE (built, fork-tested, deploy-ready)

```
katana-liquidator/
  contracts/
    src/KatanaLiquidator.sol     zero-capital Morpho callback (seize→swap→repay→sweep, minProfit gate)
    test/KatanaLiquidator.t.sol  fork tests: real Morpho path + REAL Sushi swap
    script/Deploy.s.sol          forge deploy script
    run_fork_test.sh             full-path fork harness (fetches live Sushi calldata)
  analysis/  (READ-ONLY, stdlib)
    rpc.py keccak.py multicall.py models.py   ported infra (Morpho math, offline keccak)
    protocols.py                 VERIFIED Katana addresses + market/token registry + decoders
    morpho_api.py                Morpho indexer discovery — CURRENT borrowers, no getLogs-from-0
    monitor.py                   discovery(api) + on-chain HF scanner + liquidation sizing
  bot/
    sushi.py                     Sushi v7 client: quote + RouteProcessor calldata (atomic exit)
    executor.py                  live loop: scan → evaluate(chunk vs live quote) → sign+broadcast
    deploy.sh run.sh katana-executor.service
```

**Flow:** `monitor.scan()` discovers current near-edge borrowers from the **Morpho indexer**
(`morpho_api.fetch_candidates`, HF ≤ ceiling — instant, no historical getLogs scan) → multicalls
position/market/oracle state → computes exact trigger HF (Morpho.sol `_isHealthy` math) → sizes
each liquidation. The executor `evaluate()`s each HF<1 target against a **live Sushi quote**, picks
the largest chunk whose net clears the floor (chunk-sizing under depth), then `fire()`s an atomic
`KatanaLiquidator.liquidate()`: Morpho seizes LIF collateral → `onMorphoLiquidate` swaps it via
the Sushi RouteProcessor → Morpho pulls the repay → surplus swept to owner.

### Verified on-chain (2026-07-13)
| Thing | Address / value | How verified |
|------|------|------|
| Morpho Blue | `0xD50F2DffFd62f94Ee4AEd9ca05C61d0753268aBc` | Morpho GraphQL + 15,582 bytes code |
| Sushi RouteProcessor | `0xAC4c6e212A361c968F1725b4d055b47E63F80b75` | Sushi API tx.to + 5,151 bytes code |
| Katana RPC | `rpc.katana.network` | eth_chainId → 0xb67d2 (747474) |
| LIF math | LLTV .86→4.38%, .77→7.41%, .915→2.62% | matches brief; `models.lif_from_lltv` |
| HF math | on-chain 1.048183 vs API 1.048075 (real $112k pos) | Δ0.0001 = unaccrued interest |
| liquidate() selector | `0x4bffc045` | offline keccak == `cast sig` == calldata parity |

### Fork tests (proof the whole path works)
`KATANA_RPC_URL=https://rpc.katana.network contracts/run_fork_test.sh`:
1. **Deterministic Morpho path** — seize→swap→repay→sweep + minProfit-gate revert, against the
   REAL Morpho Blue on a Katana fork (mock market for determinism). Profit realised.
2. **REAL Sushi swap** — deals real vbWBTC, runs the exact approve+call the callback uses with
   live RouteProcessor calldata on REAL pools: **1.0 vbWBTC → 61,264 vbUSDC, 0.855% impact,
   matching the quote exactly.**

### Safety (capital protection)
DRY_RUN=1 default · off-chain net gate · on-chain `minProfit` gate (2nd layer) · swap-input
drift haircut · daily-gas + consecutive-revert kill-switch · target dedup · automatic chunking.
Hot wallet holds only gas; profit is swept out; contract holds no standing funds.

### Tests
- Offline (stdlib, no network): `analysis.test_{keccak,models,protocols,monitor}`, `bot.test_executor` — **all green**.
- Fork (needs KATANA_RPC_URL): both suites in `contracts/run_fork_test.sh` — **pass**.

---

## Decision log
- **2026-07-13** Verified opportunity independently: market ~2.5× the brief ($24–28k/mo bonus),
  slippage fear refuted (real quotes: +2.48% net on a $148k exit), gas negligible, zero-capital.
  Built full stack, fork-tested real Morpho + real Sushi. Advisor tool unavailable — proceeded on
  evidence. Left the live deploy + wallet funding to the operator (key custody).
- **2026-07-14** Operator deployed KatanaLiquidator live at
  `0x25b5DeA89c8d337d0B040aBd10f8D69c2DfbCa45` (owner 0x3E8E…, morpho verified) — contract OK. The
  live DRY-run then exposed a real gap the fork test couldn't: `monitor.scan()` built the book via
  `getLogs(Borrow)` from block 0 across a 37M-block chain; the public RPC truncates wide chunked
  responses (`IncompleteRead`) and it is impractically slow. **Fix:** switched discovery to the
  **Morpho indexer** (`analysis/morpho_api.py`, `KT_DISCOVERY=api` default) — current near-edge
  borrowers instantly; exact trigger HF still on-chain. getLogs is now optional and bounded only.
  Also made `bot/sushi.py` fail-fast on `NoWay`/HTTP-4xx (dead-collateral tokens like yUSD have no
  Sushi route) via `NoRouteError`, so the executor skips them without retry churn.
  **Re-tested DRY-run on LIVE `rpc.katana.network`**: `positions 559 | targets(HF<1) 4 | guard=OK
  | contract=set` in **9s**. The 4 HF<1 targets are all dead-collateral dust (yUSD/sYUSD/wsrUSD,
  no exit) — correctly skipped; no profitable liquidation is live right now (bursty flow, as
  expected). The bot will fire the moment a real vbWBTC/vbETH position crosses HF<1.
- **Open follow-ups** (post-deploy, operator's call): reactive near-edge poll for the weETH/vbETH
  cluster (sized small); periodic `sweep(collateralToken)` for dust; watch Sushi CoL depth trend.
