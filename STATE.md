# STATE — Katana Morpho Liquidator

Battle-ready liquidation bot for Morpho Blue on **Katana** (chainId 747474). This file is the
verdict + architecture + economics + decision log. Operator handoff steps are in `README.md`.

Status as of **2026-07-13**: verification done, code built, **fork-tested end-to-end**, deploy-
ready. Stopped BEFORE live mainnet deploy (operator's key + funding = operator's step).

**Update 2026-07-16 — v2 increment 1 (LIVE) + Phase 2 (BUILT, DISABLED).**
- _Increment 1 (deployed, commit e8e0704):_ hot set capped to top-25 by debt (was ~379, the whole
  weETH/vbETH cluster), HOT_POLL_SEC 1s→0.3s. Hot pass ~4.4s→~0.5s; detection ~2.3s→~0.8s (≈ block
  time). Firing logic unchanged. Strengthens cascade-spillover capture (does NOT win contested
  singles — those are a priority-gas auction; see Phase 2).
- _Phase 2 — competitive fee-bidding (BUILT, `KT_FEE_BID=0` DISABLED, needs review + funding):_
  Katana orders single tickets by a priority-gas auction (measured competitor bids 171-443 gwei);
  the default 0.001 gwei never wins one. Phase 2 bids a margin-capped competitive priority fee.
  OFF by default → zero behaviour change until enabled. Code: `_competitive_priority_gwei` +
  `fire()`; tests `TestFeeBid`. **Risk model:** a WON bid burns ~GAS_UNITS×bid ≈ $100-1000+
  priority gas (recouped from the $300-2000+ bonus); a LOST-but-included bid burns the reverted
  gas at the bid price — measured ~125-160k gas, i.e. ~0.038-0.048 ETH ≈ **$71-90 at a 300 gwei
  bid** (the earlier "~$47" understated it). The elevated win-cost is charged to the daily gas
  kill-switch UP FRONT (conservative). **Knobs (set before enabling):** `KT_FEE_BID=1`,
  `KT_FEE_BID_MIN_NET_USD=300` (only bid above this net), `KT_MAX_PRIORITY_GWEI=600` (hard bid
  cap), `KT_FEE_BID_KEEP_USD=50` (min net kept after the bid), and **`KT_MAX_DAILY_GAS_USD`
  MUST be raised** from $10 or one bid trips the kill-switch.
  **Funding (corrected 2026-07-16 — the earlier "~$50-100" was 10-40× short):** the node REJECTS
  a bid outright unless the EOA holds the FULL fee envelope, balance ≥ GAS_LIMIT(1,800,000) ×
  maxFeePerGas (= 2×base + bid; Katana base ~0.001 gwei, negligible — the bid IS the fee).
  Required balance by bid (@ ETH ≈ $1,878):

  | bid (priority gwei) | required balance | ≈ USD |
  |---|---:|---:|
  | 148 — minimal competitive (net=$300 ticket) | 0.27 ETH | ~$500 |
  | 300 — mid-auction (observed 171-443) | 0.54 ETH | ~$1,014 |
  | 600 — `KT_MAX_PRIORITY_GWEI` cap | 1.08 ETH | ~$2,028 |

  **Go-live (after review + funding):** (1) fund the Katana wallet
  `0x3E8E4B5EB633F5e3CdC5657A3BD16f01c080C4D5` (shared w/ WC) per the table — the cap row if the
  600 gwei cap stays. The executor now checks the EOA balance at startup + every ~10 min
  (`check_balance`, alert below the fire-readiness floor); a clean balance preflight is a go-live
  precondition. (2) set the knobs in `~/.katana-bot/env`; (3) restart (kill→cron), verify the
  banner + first contested fire in TG. Kill-switch + on-chain minProfit still bound the downside.

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
    fastpath.py                  block-phase lock + flip thresholds for the pre-armed fire (v3)
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

**Update 2026-07-15 (2) — внешнее ревью применено (worktree-review-fixes).** Полное ревью кода
(6 измерений + сверка с Morpho.sol) нашло 4 критических + 10 высоких. Применено в этом коммите:
- **C1** float-переполнение repaidShares >2^53: chunk-фракции теперь рационалы, всё сайзинг —
  точная целочисленная математика (иначе полный клоуз $3.4M weETH/vbETH детерминированно
  Panic(0x11) → 3 реверта → kill-switch). Тест с шарами 1e27.
- **C2** startup-preflight: eth_abi/eth_account/cast/код контракта/chainId проверяются на старте
  loop, фейл = громкий alert + exit(1) (раньше ModuleNotFoundError глотался как «loop err» —
  eth_account реально не был установлен на VPS при DRY_RUN=0!).
- **C3** accrual: HF считается с довесом процентов (irm.borrowRateView + wTaylorCompounded, порт
  MathLib) — на тихих рынках stored-HF опаздывал на дни процентов (дреги: 0.70→0.51). Плюс
  preflight eth_call точной calldata перед отправкой (ловит lost race + гоняет реальный
  _accrueInterest+_isHealthy за ~10мс бесплатно).
- **C4** kill-switch: sys.exit(1), алерты троттлятся (cron воскрешает каждую минуту), TG-токен
  из KT_TG_TOKEN/файла с кэшем, KT_CHAT_ID теперь обязательный env (дефолт пуст + warning).
- **H1** профит-флор для не-стейбл займов (vbETH): USD-флор через живой ETH_USD (Sushi-квота,
  5мин TTL; захардкоженные $3300 были в 1.7 раза выше рынка), on-chain minProfit =
  max(usd_floor, net/2) везде. Approx-USD долга для ETH/BTC-займов → MIN_DEBT-гейт работает.
- **H2** Telegram-алерты асинхронные и ПОСЛЕ broadcast (блокирующий sendMessage стоял между
  решением и отправкой, до 20с). **H3** RAW_TX=1 дефолт (in-process подпись; cast — фолбэк,
  ключ через env, не argv). **H5** классификация ревертов: «position is healthy»/Panic(0x11) =
  lost_race, НЕ инкрементит kill-switch. **H6** dedup: успех блокирует 10с (остаток чанкованной
  ликвидации перезабирается сразу), pending — до DEDUP_SEC. **H7** ресипт ждём 20с → pending
  трекается и класифицируется следующими пассами; send-фейл ≠ revert. **H8** quote timeout 5s /
  2 ретрая / дедлайн evaluate 10с; 429/408 Sushi ретраятся (были NoRoute→скип таргета).
  **H9** keep-alive на write-пути. **H10** hot-тик: Rpc retries=2, ротация стартового
  эндпоинта по номеру пасса. **M2** capped-close: −0.5% к шарам. **M3** перечитка оракула перед
  файром (>0.2% вниз = скип тика). **M6** позиции+цены+ставки+timestamp в одном aggregate3.
  **M7** ручной `once` принудительно DRY_RUN=1 (KT_FORCE_LIVE_ONCE=1 для обхода). **M8**
  KT_LIQ_LOG_WINDOW=2000: чужие Liquidate → «RACE» алерт + races_lost в heartbeat. Газ
  списывается по фактическому gasUsed из ресипта.
Отложено (требует редеплой контракта): M4 авто-sweep collateral-пыли (0.3% сеиза оседает в
контракте), M2-полный (режим seizedAssets), событие с marketId. Отложено (инфра): WSS/платный
RPC, параллельный сабмит на несколько ингрессов, нонс-реплейсмент застрявших транз.

**Update 2026-07-15 (3) — M4+M2 контракта: редеплой.** liquidate() принимает
(seizedAssets, repaidShares) как Morpho (ровно один ненулевой): collateral-capped закрытия
стреляются режимом seizedAssets (пин сеиза −0.3%, Morpho сам выводит repaid по цене
исполнения — Panic(0x11) на тике исключён по построению, M2); в конце liquidate() досвипается
и collateralToken (хэйркат-пыль ~0.3% сеиза больше не копится в контракте, M4). Событие
Liquidated теперь несёт seized/repaid; setOwner с zero-check; Deploy.s.sol требует chainid
747474. Бот: LIQUIDATE_SELECTOR 0x79755efe (сверен cast==оффлайн-keccak), evaluate ветвится
capped/uncapped, _shares_for_repaid без 0.5%-шейва (не нужен). Форк-тесты: 6/6 против
реального Morpho (вкл. capped-close и dust-sweep); юниты 17/17.

**Update 2026-07-17 — v3 latency: предиктивный детект границы блока + pre-armed fire.**
Пробы (`~/.katana-probe`, 551 проба): секвенсер включает tx в СЛЕДУЮЩИЙ блок только если она
ПРИШЛА в ~0.25–0.35с после появления блока N — P(next) 21% @+0.05s, 9–13% @+0.15–0.25s, ~0%
@≥0.35s; send one-way ~110–150мс. Старый детект (фикс-каденс + холодный urllib) съедал
0.65–1.05с сам по себе → end-to-end 1.5–2.1с, P(B0+1) ~7–10%. Сделано (3 коммита):
- **Keep-alive транспорт** (`analysis/rpc.py`): модульный пул персистентных соединений (один
  сокет на эндпоинт; reconnect-once на протухший LB-сокет ~60с idle), `warm()` перед боевым
  окном и каждый пасс; `poll_block_number()` — one-shot без пейсинга/ретраев для tight-poll.
  Тёплый RTT 21–26мс против ~115мс холодного; hot-pass 0.88 → 0.40–0.65с.
- **Фазовый замок на блок-тик** (`bot/fastpath.py`, `KT_PREDICTIVE_POLL=1` дефолт): idle-зона
  [t0, t0+0.80с) — ВЕСЬ обслуживающий трафик (hot-pass, квоты, пре-подпись, warm-пинги);
  armed-зона — tight-poll `eth_blockNumber` каждые ~18мс → детект ≈ step/2+RTT. Якорь только
  по НАБЛЮДЕННОЙ границе (ошибка ≤ step+RTT, без дрейфа); поздний вход (медленный пасс)
  поглощается предсказанным якорем (каданс 1.000с, макс 2 подряд); слом паттерна — громкий
  фолбэк на классический hot-каденс c ресинком.
- **Pre-armed fire** (`bot/executor.py`): цели с 1 ≤ HF < `KT_ARM_HF`=1.002 (топ-4 по долгу,
  MIN_DEBT-гейт) готовятся в idle-зоне: живые шары + ТОТ ЖЕ `evaluate()` (экономика байт-в-байт)
  + calldata + sanity-preflight («position is healthy» обязателен; любой другой реверт = не
  армить; preflight ПРОШЁЛ = уже флипнулась → классический fire немедленно) + нонс + пре-подпись
  (свежесть `KT_ARM_QUOTE_TTL`=2.5с). На границе блока: ОДИН заранее собранный aggregate3 по
  ораклам hot-сета → целочисленный флип-порог (бит-в-бит `_isHealthy`, тест ±1 wei на >2^53) →
  отправка пре-подписанной raw. **Blind fire ТОЛЬКО на дефолтном типе 0.001 gwei** (проигранная
  гонка ≈ $0.001); цель с Phase-2 бидом всегда держит preflight в критическом пути (реверт бида
  сжигает бид); `KT_BLIND_FIRE=0` = preflight для всех. Сожжённый нонс (fires_at_sign) блокирует
  слепую отправку; фейл fast-send рефандит fires/gas и НЕ кулдаунит цель (классика ретраит ~1с).
Замерено вживую (DRY_RUN, стейт в памяти): замок держится (межблок 1.000с), detect→flip-check
p50 120мс (мин 53мс) + send one-way ~110–150мс ⇒ detect→секвенсер ~0.2–0.27с — целевая полоса
P(B0+1) 13–21% (было 7–10%). Тесты: 134/134 зелёные (было 72). Гарантии не тронуты: kill-switch,
дедуп, флоры, sizing, бид-математика Phase 2 — тот же код, лишь вынесен раньше по времени.
