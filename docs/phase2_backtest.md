# Phase-2 funding gate — historical backtest of the Katana liquidator

**Date:** 2026-07-20 · **Script:** `analysis/phase2_backtest.py` · **Quoter:** `analysis/phase2_quoter.hex`
**Scope:** all 95 historical liquidation tickets with prize ≥ $300 in our 6 markets
(Morpho Blue on Katana, chainId 747474, deploy block 2 741 069 → head ~37 784 468, 406 days).
**Nothing was signed or sent.** Read-only `eth_call` / `eth_getBlockByNumber` only.

---

## 0. TL;DR for the funding decision

| Question | Answer |
|---|---|
| Were the borrowers **visible** to our scanner before the competitor's tx? | **86/95** had HF<1 at `blk−1`; **95/95** were inside the hot-set (HF<1.05) |
| Would `evaluate()` have **assembled a profitable route**? | **36/95 (38%)** overall — but see the split below |
| … excluding one single whale borrower | **31/31 (100%)** |
| … on that one whale borrower | **5/64 (8%)** |
| Of the 36 reachable, how many clear the Phase-2 $300 gate? | **35/36** |
| Median net on reachable tickets | **$1 721** (total $93 853) |
| Median net **after paying a competitive bid** | **$433** (total $54 363) |

**The headline 38% is misleading and must not be read as "the route is broken".** There are 59
failures, and **all 59 belong to one borrower** — a single $16M position (`0x14bcd9da05…`)
drip-liquidated in tiny slices through November 2025. **Outside that one borrower the failure
count is zero: our own `evaluate()` assembles a profitable, impact-compliant route on all 31
remaining tickets.**

So: for the ordinary ticket flow, **route quality is not the blocker — the bid is.** That is the
condition under which the brief says funding is justified. But the whale cluster, which is
**47% of all prize dollars ($97 812)**, is lost to a *code* defect (chunk-ladder granularity),
not to the bid — and fixing it costs nothing but a code change. Recommendation in §6.

---

## 1. Method — and why this is a measurement, not an estimate

### 1.1 Archive availability (checked first, as instructed)

The public Katana RPC **does serve archive state**. Verified not with a constant (`owner()`
returns the same bytes at every height and proves nothing) but with a value that must change:
`Morpho.market(id)` returns materially different totals at blocks 6.0M / 12.1M / 16.7M / 33.8M /
37.0M. No `missing trie node` / `state not available` at any depth tested, back to block 2.7M.

### 1.2 The route problem, and which option was taken

The brief offered three options. **Option (a) as literally written is impossible**: production
`evaluate()` (`bot/executor.py`) prices the exit through the **Sushi v7 HTTP API**
(`bot/sushi.py`), and that API has no historical mode. Our quoter is off-chain, so there is no
"same quoter" to call archivally.

However a strictly better path than (b) or (c) exists, and it is what was used:

1. Every pool the Sushi router actually routes our 6 pairs through was extracted **from the
   router's own `tx.data`** and classified on-chain. **All of them are Uniswap-V3-style pools**
   (they answer `slot0()` / `liquidity()` / `fee()`); there is no V2 or stable-swap leg.
2. The node supports `eth_call` **`stateOverride`**, including at historical blocks.
3. So a minimal **V3 path quoter** (source in `analysis/phase2_backtest.py` header, runtime in
   `analysis/phase2_quoter.hex`) is injected at a scratch address and runs **real V3 swap
   simulations against the historical tick state at `blk−1`**. The swap is always rolled back by
   a reverting `uniswapV3SwapCallback`, so it can never touch chain state.

This is **option (a) done properly** — a real quoter, real archival state — not a reserves
approximation and not a size-only upper bound.

### 1.3 Fidelity check (this is what makes it trustworthy)

The injected quoter was validated against the **live** Sushi API at `latest`, same amounts:

| pair | quoter | Sushi API | ratio |
|---|---:|---:|---:|
| vbWBTC→vbUSDC 0.1 | 6 388 369 749 | 6 388 723 894 | 0.9999 |
| vbWBTC→vbUSDC 1.0 | 63 520 809 929 | 63 528 570 271 | 0.9999 |
| vbETH→vbUSDC 10.0 | 18 389 396 269 | 18 403 432 295 | 0.9992 |
| vbETH→vbUSDT 5.0 | 9 221 852 453 | 9 230 637 624 | 0.9990 |
| weETH→vbETH 10.0 | 10 979 994 841 143 781 362 | 10 979 994 841 143 789 568 | 1.0000 |
| LBTC→vbUSDC 0.1 | 6 406 527 678 | 6 407 053 430 | 0.9999 |
| vbWBTC→vbUSDT 0.5 | 31 870 915 645 | 31 872 834 341 | 0.9999 |

Ratios **0.9990 – 1.0000, always ≤ 1**. The reconstruction is faithful and very slightly
**conservative** (we take the best single path; the real router may split across pools and can
only do better). Reported nets are therefore, if anything, understated.

### 1.4 What the replay reproduces exactly

* **HF** — `Morpho.position/market/idToMarketParams` + `oracle.price()` at `blk−1`, with pending
  interest accrued via `irm.borrowRateView` + `wTaylorCompounded` exactly as `liquidate()`
  accrues **before** `_isHealthy` (`analysis/monitor.py` scan path).
* **Sizing** — `size_liquidation()`: full close capped by collateral value / LIF.
* **`evaluate()`** — the same `CHUNK_FRACTIONS` ladder `(1, 3/4, 1/2, 7/20, 1/4, 3/20, 1/10, 3/50)`,
  the same 0.3% swap-input haircut, the same integer math, the same gates:
  `MIN_PROFIT_USD=$20`, **`MAX_IMPACT=2%`**, `MIN_DEBT_USD=$500`.
  Price impact is re-derived from our own quote curve against a ~0.001%-of-full mid reference,
  since Sushi's `priceImpact` field is not historically available.
* **Partial fills** — each hop is bounded to a 20% adverse price move. Without that bound V3
  happily consumes the entire input and drains the pool at an absurd price, which would have
  *masked* the "router cannot fill this size" case. With it, `consumed < requested` means exactly
  Sushi's `Partial` status, and the ladder chunks down just as it does live. (The bound is
  economically free: the LIF bonus is ~4.4%, so any chunk moving a pool 20% is hopeless anyway.)
* **Gas** — real `baseFeePerGas` at `blk−1` × `GAS_UNITS_EST=900 000` × ETH/USD read from the
  vbETH/vbUSDC pool **at that block**.

---

## 2. Visibility (question 1)

State read at `blk−1`, i.e. strictly **before** the competitor's liquidation.

| | count |
|---|---|
| HF < 1 (a live target) | **86 / 95** |
| Inside the hot-set, HF < `KT_HOT_WATCH_HF` = 1.05 | **95 / 95** |
| Full target (HF<1 **and** debt ≥ $500) | **86 / 95** |

HF range 0.982747 … 1.009883, median 0.998294.

**All 95 borrowers were already in the hot-poll set** before the ticket existed — discovery and
the ~0.3s hot cadence were never the limiting factor.

The **9** tickets with HF ≥ 1 at `blk−1` (prize $31 777) only became liquidatable *in the
liquidation block itself* — an oracle tick inside that block created and consumed the
opportunity. Those are precisely the tickets the predictive/fastpath machinery
(`bot/predict.py`, `bot/fastpath.py`) exists for; they cannot be caught by any `blk−1` poll.
Notably all 9 were route-reachable, and 6 of them are the whole `vbWBTC/vbUSDT` sample.

---

## 3. Route assembly (question 2 — the main one)

Production gates applied in full, including `MAX_IMPACT`:

| | rows | prize $ | prod net $ |
|---|---:|---:|---:|
| **All tickets** | 36 / 95 | 206 632 | 93 853 |
| **Excluding whale `0x14bcd9da05…`** | **31 / 31** | 108 820 | 85 492 |
| **Whale `0x14bcd9da05…` only** | 5 / 64 | 97 812 | 8 360 |

The whale's 64 rows split across two markets: **59 in vbWBTC/vbUSDC, every one of which failed**,
and 5 in vbWBTC/vbUSDT, every one of which succeeded (its vbUSDT debt there was small enough to
close within pool depth). All 59 of the backtest's failures are therefore one borrower in one
market.

* Median net on reachable tickets **$1 721**, max **$13 966**.
* Chosen-chunk impact: median **0.82%**, max 1.89% — comfortably inside the 2% cap.
* Chunk fraction actually used: `f=1.0` on 26 tickets, `0.75`×2, `0.35`×1, `0.15`×1, `0.06`×6.
* **35 of 36 clear the Phase-2 $300 net gate.**
* **No ticket failed for lack of a route** — a fillable size existed in every single case.
  Every failure is a *sizing* failure, not a liquidity-absence failure.

### 3.1 Why the whale cluster fails — a real, fixable defect

Borrower `0x14bcd9da05…` held ~$16M of debt against ~196 vbWBTC. Our ladder sizes chunks as a
fraction **of a full close**, and its floor is `3/50` = 6%. Six percent of that position is
~10.6 vbWBTC — still **~30×** more than the vbWBTC/vbUSDC pool could absorb at the time. So
`evaluate()` walks the entire ladder, finds every rung unfillable, and gives up.

Measured quote curve at `blk 16693558` (ticket prize $423):

| input | output | implied BTC px |
|---:|---:|---:|
| 176.46 vbWBTC (f=1) | 115 869 vbUSDC | $657 |
| 10.59 vbWBTC (f=3/50, ladder floor) | 115 575 vbUSDC | $10 916 |
| 0.353 vbWBTC (f=0.002) | 30 192 vbUSDC | **$85 548** ← true price |

The winners simply took **small absolute slices**. One competitor is explicitly visible doing
this: tx `0x1ca415902d86…` contains **four separate `Liquidate` logs on the same borrower in a
single transaction** (verified on-chain via the receipt) — i.e. they chunked *below* our floor
and batched the slices. Two further txs (`0x52fff1c6b13a…`, `0x0cbd64c3ffab…`) do the same.

**Diagnostic — how much a finer sizer would recover.** Extending the ladder geometrically down
to 0.02% of a full close (same profit and impact gates, nothing else changed):

* profitable on **63 / 95** rows instead of 36;
* recovers **27 tickets production currently misses**, worth **$22 399** (median $158);
* on the whale specifically: **32 / 64** instead of 5 / 64.

This is a diagnostic of what better sizing is worth — *not* a claim about the current bot.

---

## 4. Breakdown

### By market

| market | n | visible | prod route | deep route | prize $ | prod net $ |
|---|---:|---:|---:|---:|---:|---:|
| vbWBTC/vbUSDC | 66 | 66 | 7 | 34 | 108 974 | 27 229 |
| vbETH/vbUSDC | 17 | 16 | 17 | 17 | 67 647 | 52 295 |
| vbWBTC/vbUSDT | 6 | 0 | 6 | 6 | 21 103 | 8 997 |
| vbETH/vbUSDT | 4 | 3 | 4 | 4 | 6 258 | 3 248 |
| LBTC/vbUSDC | 2 | 1 | 2 | 2 | 2 649 | 2 084 |
| weETH/vbETH | 0 | — | — | — | 0 | — |

`vbWBTC/vbUSDC` carries the whale. Every other market is **100% route-reachable**.
`weETH/vbETH` never produced a ≥$300 ticket — its 2.6% LIF on a correlated pair is too thin.

### By month

| month | n | visible | prod route | deep route | prize $ | prod net $ |
|---|---:|---:|---:|---:|---:|---:|
| 2025-09 | 1 | 1 | 1 | 1 | 8 776 | 7 139 |
| 2025-10 | 6 | 6 | 6 | 6 | 24 874 | 33 255 |
| 2025-11 | 64 | 64 | 5 | 32 | 85 238 | 6 765 |
| 2026-01 | 6 | 4 | 6 | 6 | 21 628 | 12 663 |
| 2026-02 | 8 | 7 | 8 | 8 | 12 757 | 10 387 |
| 2026-06 | 10 | 4 | 10 | 10 | 53 359 | 23 643 |
| 2026-07 | 0 | — | — | — | 0 | — |

November 2025 is not 64 opportunities — it is **one stressed whale, 59 of the 64 rows**. Every
other month is fully reachable. The flow is episodic and event-driven, as previously measured.

---

## 5. The bid, and what we deliberately did **not** conclude

Applying `_competitive_priority_gwei()` to the reachable tickets (`FEE_BID_MIN_NET_USD=$300`,
`FEE_BID_KEEP_USD=$50`, `MAX_PRIORITY_GWEI=600`):

* median net **after** paying the bid: **$433**; total $54 363;
* 19 of 36 keep ≥ $300 after the bid; all 36 keep ≥ $50 (that floor is by construction);
* on **35 of 36**, our affordable bid ≥ the tip the winner actually paid.

> ### Honesty boundary — read this before using the number above
>
> **"Our affordable bid ≥ the observed winner tip" does NOT mean we would have won.** The
> observed tip is the clearing price of that ticket **in our absence**. Had we bid, the winner's
> best response would have been a higher bid, and the auction would have cleared somewhere else
> entirely. This backtest answers **"could we have participated on economically sound terms?"**
> and never **"would we have won?"** — the latter is not recoverable from historical data by any
> method, and no number in this document should be read as a win-rate.
>
> Likewise, the net totals ($93 853 / $54 363) are **upper bounds on that segment**: they assume
> we won every ticket we could have contested, which will not happen.

---

## 6. Verdict

**On the brief's own decision rule.** The rule was: *if most tickets fail to assemble a route,
the bid is useless and funding must not proceed; if they assemble, the bid is the only obstacle.*

The answer is **structurally split, and the split is the finding**:

1. **For the ordinary ticket flow (31 of 31, 100%) the route assembles cleanly** — median net
   $1 721, impact 0.82%, 35/36 clearing $300, and $433 median retained after a competitive bid.
   On this segment the bid genuinely **is** the only remaining obstacle. The brief's condition
   for funding is met here.
2. **For the single whale cluster (64 rows, 47% of all prize dollars) the bid is irrelevant** —
   we would never have reached the auction, because `evaluate()` cannot size a chunk small
   enough. No amount of ETH in the bid wallet buys that ticket.

**Recommendation: fund Phase 2, but do not fund it *first*.** The chunk-ladder fix is strictly
cheaper than 0.6 ETH, strictly lower-risk (no capital at stake), and on this sample worth
**+27 tickets / +$22 399** that the bid cannot touch. Concretely:

* extend `CHUNK_FRACTIONS` well below `3/50`, or better, **size chunks in absolute terms against
  measured pool depth** rather than as a fraction of a full close (the fraction basis is what
  breaks on whales — a 6% floor is meaningless when the position is 30× the pool);
* consider batching several small slices per tx, which is demonstrably what the incumbent
  winners already do;
* then fund the bid envelope, whose value is now measurable on a segment we can actually reach.

**Caveat on ordering:** these two are complementary, not alternatives. Funding the bid alone
still leaves ~half the prize pool unreachable; fixing sizing alone still loses every contested
ticket to a higher bidder. But sizing is free to fix and should land first.

---

## 7. Reproducing

```bash
python3 analysis/phase2_backtest.py          # all 95 tickets (~8 min, public RPC)
python3 analysis/phase2_backtest.py 5        # first 5 only
```

Inputs (from the history scan, job `d9c2c3f6`): `all_liqs.json`, `prized.json`, `big_tips.json`
in `$KT_BT_JOB` (default `/home/claude-agent/.claude/jobs/d9c2c3f6/tmp`).
Output: `$KT_BT_OUT` (default `<job>/phase2_backtest.json`), one record per ticket with HF,
sizing, chosen chunk, impact, net, bid and the full quote ladder. The run is resumable — an
existing output file is reused per-`tx`.

Rebuilding the quoter runtime (`analysis/phase2_quoter.hex`) requires the Solidity source in the
`analysis/phase2_backtest.py` module docstring plus `forge inspect PathQuoter deployedBytecode`.

### Known limitations

* **Pool set is today's.** Paths were discovered from the router's *current* calldata. Had a
  historically-relevant venue since been removed, we would understate proceeds. No ticket failed
  for want of any route, so this did not bite — but it is not proven absent.
* **Best single path, no router splitting** → proceeds understated (conservative).
* **Our sizing ≠ the winner's sizing.** We model a full close at `blk−1`; winners often took a
  smaller slice. Our net therefore sometimes *exceeds* the observed prize (e.g. blk 13388076:
  prize $9 048, our net $13 966). Both figures are reported side by side; they are not the
  same quantity and should not be differenced.
* **95 rows = 92 transactions.** One tx carries 4 `Liquidate` logs on one borrower (§3.1), so a
  few rows describe slices of a single race rather than independent opportunities.
* **No mempool/latency modelling.** Inclusion order, propagation and the competitor's best
  response are all out of scope — see the honesty boundary in §5.

---

## 8. Postscript — the sizing defect is fixed (re-measured)

Everything above measures the bot **as it stood at HEAD 254a652**. The §3.1 defect has since been
fixed in `bot/executor.py`: below the `3/50` ladder floor `evaluate()` now descends by halving,
bounded from below by `f_min = (MIN_PROFIT_USD + gas_usd) / full_prize_usd` — the fraction under
which no chunk can clear the profit gate, since `net(f) ≈ f × full_prize − gas` — and by a hard
`MIN_CHUNK_FRACTION = 0.0002`. The descent is generated lazily, so a position whose ladder fills
costs exactly the round-trips it always did.

Re-running this same script with that sizer (`analysis/phase2_backtest.py`, unchanged
methodology, same 95 tickets):

| | before | after |
|---|---:|---:|
| route assembles | 36 / 95 | **63 / 95** |
| … whale `0x14bcd9da05…` | 5 / 64 | **32 / 64** |
| … all other borrowers | 31 / 31 | 31 / 31 |
| prod net total | $93 853 | **$112 875** |
| clearing the Phase-2 $300 gate | 35 | 44 |

The 36 tickets that already assembled are **unchanged, row for row** — same chunk fractions
(`f=1.0`×26, `0.75`×2, `0.35`×1, `0.15`×1, `0.06`×6), same $93 853 — which is the regression
proof that the fast path did not move. The 27 newly-reachable tickets are won at
`f ∈ {0.0075, 0.00375, 0.001875, 0.000937, 0.000469}`, all below the old floor, for $19 022 net
(median $94; 9 of them clear $300). Max impact across all 63 is 1.92%, still inside the 2% cap.

Two honest deltas from the §3.1 diagnostic, which projected 63 rows / $22 399 / median $158:

* the **row count matches exactly (63)**, and **no ticket the ideal fine-grained ladder could
  reach is still missed** — the economic bound `f_min` prunes only provably-hopeless sizes;
* the **dollars are ~15% lower** ($19 022 vs $22 399) and so is the median. This is expected and
  was chosen: the diagnostic took the *best* rung on a 17-point ladder, while the production
  descent halves and takes the *first* rung that fills and pays, landing within 2× of the
  boundary rather than on it. Each extra rung is a ~0.3s Sushi round-trip, and `evaluate()` also
  runs on the arm path under `deadline_mono`; buying that last ~$3 400 across 406 days with
  finer steps would cost latency in every race. Worst case the descent adds 8 rungs (16 total);
  the `_partial_known` memo makes all rungs above a known-Partial size free on later passes.

**This does not change §5 or §6.** Reachability is not a win-rate — these 27 tickets now merely
reach the auction. The funding argument is unchanged except that the segment the bid can act on
is now materially larger.
