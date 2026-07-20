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

---

**Update 2026-07-17 — mempool same-block backrun (SHADOW дефолт) + 2 мелких правки.**
Conduit-нода `wss://rpc.katanarpc.com` (op-reth) — нетипично для OP-стека — держит ПУБЛИЧНЫЙ
мемпул и `eth_subscribe`. Оракульный price-update виден PENDING до включения; op-reth сортирует
по УБЫВАНИЮ эффективного prio-fee, ничьи — FCFS по приходу. На 8/8 same-block победах prio
победителя == prio оракульного пуша ДО WEI. Отсюда same-block backrun: наша ликвидация с
`maxPriorityFeePerGas` РОВНО равным типу пуша встаёт СРАЗУ ЗА ним в ТОМ ЖЕ блоке (пуш двигает
цену → наша tx после него видит новую цену и ликвидирует). Промах реверт на ~$0.01 газа —
та же экономика, что blind-fire на низком типе. Слоёв (аддитивно к v3 next-block, за флагами):
- **WSS-менеджер** (`bot/mempool.py`, `KT_MEMPOOL=1`): фоновый поток, персистентный сокет с
  reconnect+backoff, подписки newHeads + newPendingTransactions. Свой транспорт и своя read-
  линия для тел tx — НЕ трогает пул `analysis.rpc` и не пишущую линию. Главный цикл никогда не
  блокируется на сокете (lock-снапшот головы + колбэк). Обрыв WSS = громкий лог + reconnect;
  предиктивный поллинг next-block работает независимо (мёртвый WSS = «нет same-block попыток»).
- **Оракульный конфиг** (`bot/oracles.py`): зеркало read-only ценза (`infra_oracle_census/
  _feeds`, 2026-07-17) — Chainlink-агрегаторы (BTC/ETH/LBTC/USDC/USD, WBTC/BTC) + комитеты
  трансмиттер-EOA на фид + какие фиды двигают каждый из 6 рынков. `markets_for_tx(to,from)`:
  агрегатор `to` однозначен (1 фид), трансмиттер `from` широкий (комитеты общие) → объединение
  кандидат-рынков (graceful degrade: армим все hot-цели в них, флип-чек подтверждает по факту).
- **Same-block отправка** (`bot/executor.py`): на пуш — по каждой pre-armed цели рынка пере-
  подписываем calldata с типом пуша (matched-to-wei, замороженные нонс+base) и шлём на
  ВЫДЕЛЕННОЙ пишущей линии. Клейм fires/нонса под `_fire_lock`+fires_at_sign, поэтому same-
  block и next-block `_fire_fast` не могут дважды потратить нонс. Сеттл — через pending-запись
  главному циклу (`_check_pending`). Безопасность: fee-bid-тикет (бид > типа пуша) и тип выше
  потолка `KT_MEMPOOL_MAX_TIP_GWEI`=0.5 НЕ шлются слепо (держат preflight/уходят в next-block)
  — никогда не «слепой fire на высоком типе».
- **SHADOW дефолт** (`KT_MEMPOOL_SHADOW=1`): всё КРОМЕ `eth_sendRawTransaction` — строка
  `MEMPOOL …`. Реал-firing: `KT_MEMPOOL_SHADOW=0 KT_MEMPOOL_LIVE=1` после ревью shadow-данных.

Грамматика shadow-лога (для grep-анализатора; `MEMPOOL ` + key=value, `-`=нет значения):
  `event=signal`      — увиден пуш: market, tip_wei, tip_gwei, oracle_tx, detect_ms, head_block,
                        head_age_ms, n_armed.
  `event=shadow_fire` — какую armed-цель бэкранули бы: +market_id, borrower, hf, would_send_ms,
                        blind, send_ms_est(=KT_MEMPOOL_SEND_MS 216), head_age_ms, budget_ms,
                        feasible(0|1). `shadow_skip` (+reason) — если fee-bid тикет.
  `event=landed`      — резолюция: oracle_tx, landed_block, detect_head, blocks_after.
  live-режим: `event=live_fire`(+txh) / `live_skip`(+reason) / `live_miss`(+reason).
budget_ms = (BLOCK_SEC*1000 − head_age_ms) + CUTOFF_MS − (would_send_ms + SEND_MS); feasible =
budget_ms>0 — оценка «успели бы в блок пуша». Правда — из `landed` (реальный блок включения).
ЧЕСТНАЯ ОГОВОРКА: наш ~216мс write до US-секвенсера vs окно ~0.25–0.35с — same-block на грани;
shadow это и измерит (budget_ms + landed по факту) ДО включения live.

**Мелочь 1 — arm-квота Sushi:** пре-арм квота больше не стартует, если не влезает в остаток idle-
бюджета (таймаут = min(QUOTE_TIMEOUT, остаток), 1 попытка, флор `KT_QUOTE_MIN_TIMEOUT`=0.35с).
Убирает «evaluate deadline exceeded, giving up this pass» на weETH/vbETH (Partial-квоты).
**Мелочь 2 — тихие гонки:** каждая проигранная гонка ЛОГируется с призом (`(LIF−1)*repaid_usd`,
без RPC) и тегом (below_floor/tracked_lost/not_tracked), но пингует TG только если приз
неизвестен (fail-open) или ≥ `KT_RACE_ALERT_MIN_USD` (дефолт = профит-флор). `KT_RACE_ALERT_MAX`
кэпит пинги за пасс. Дустовые гонки (репэй ~$5.30, бонус ~$0.23) больше не спамят.

## v5 — ORACLE-PUSH PREDICTION pre-arm (bot/pricefeed.py + bot/predict.py; `KT_PREDICT`, дефолт OFF)

ИЗМЕРЕНО (2026-07-17): Chainlink BTC/ETH на Katana пушат он-чейн, когда офф-чейн цена (прокси —
Binance spot) уходит ~0.5% (или 24ч heartbeat). Офф-чейн цена пересекает 0.5% на МЕДИАНУ ~30-40с
(мин ~13-26с) РАНЬШЕ он-чейн пуша — пуш отстаёт на OCR-раунд+консенсус+tx. Это 60-80x нашего
mempool-хедстарта (~0.6с). Смотрим Binance, предсказываем пуш → успеваем быть ПОЛНОСТЬЮ pre-armed,
превращая same-block ПРОИГРЫШИ на быстрых крупных движениях (самые ценные ликвидации) в выигрыши.
FP ~46% (Binance single-venue шумнее node-медианы Chainlink), recall ~72%.

ВАЛИДИРОВАНО НА ЖИВОМ ПОТОКЕ (2026-07-19, shadow, окно 24.9ч, покрытие 100%):
lead медиана **46с** (p25 31, p90 221, min 9) — ресёрчевые 30-40с подтверждены и даже с запасом;
FP **44%** (20 ложных взводов из 45, суммарно 88 мин впустую, ~8.7% времени во взведённом
состоянии) — ресёрчевые ~46% подтверждены; recall **89%** (25 из 28).

Recall СВЕРЕН ПО БЛОКЧЕЙНУ, а не по самоотчёту: наивная формула `confirmed/(confirmed+push)`
берёт знаменателем только ЗАМЕЧЕННЫЕ пуши — пуш при лежащем боте не попал бы никуда, и цифра
льстила бы. Собраны ВСЕ `AnswerUpdated` агрегаторов BTC/USD (`0x56ac2b1b…`) и ETH/USD
(`0x47522e72…`) за то же окно: on-chain 28 (BTC 7, ETH 21) против 25 confirmed + 3 push = 28 в
логе. Совпало, пропущенных нет ⇒ 89% честные. `analysis/predict_analyze.py` считает это сам
(`--no-chain` для офлайна) и печатает покрытие.

Перекос фидов: ETH 20 из 25 confirmed, BTC 5. Один случай lead=9с — окно, где подготовиться
почти нереально.

ЧЕГО ЭТИ ЦИФРЫ НЕ ГОВОРЯТ (главное для решения `KT_PREDICT_LIVE`): они меряют КАЧЕСТВО
ПРЕДСКАЗАНИЯ, а не заработок. `shadow_fire` = 0 при 60 сигналах mempool-шэдоу (`armed>0` в 0 из
60): в момент пуша у края НЕ БЫЛО позиции, которую выгодно взять. Предсказание работает,
экономика — нет. Включать live не на что, пока shadow_fire не покажет реальные срабатывания.

ПРИНЦИП (нельзя нарушать): предсказание — это edge на ПОДГОТОВКУ, НЕ на обгон. Мы НЕ МОЖЕМ
выстрелить до он-чейн пуша (позиция не ликвидируема, пока оракул не переоценил он-чейн, а точный
tip раунда всё равно читается из pending oracle tx в мемпуле). Поэтому предикт-слой НИКОГДА сам
ничего не шлёт — только PRE-ARM (расширяет pre-signed флип-сет для рынков движущегося фида, греет
write-линию). Реальный fire — как раньше: v4 mempool-слой на ПОДТВЕРЖДЁННОМ pending oracle tx
matched-tip и broadcast. Спекулятивный fire на предсказании ЗАПРЕЩЁН (46% FP → реверт/газ впустую).
- **Binance WS** (`bot/pricefeed.py`): фоновый daemon-поток, персистентный сокет (переиспользует
  `WsConn`/фрейминг mempool.py) на `wss://stream.binance.com:9443/ws`, in-band SUBSCRIBE обоих
  `btcusdt@bookTicker`+`ethusdt@bookTicker` (combined `/stream?streams=` НЕ используем — WsConn
  роняет query-string). Свой lock-снапшот mid + `healthy()`; reconnect с capped backoff. WS упал =
  предсказаний нет, mempool/fast-path не затронуты. ПРОВЕРЕНО с этого VPS: 101-хэндшейк ~1с, оба
  символа текут на одном коннекте.
- **Anchor/return** (`bot/predict.py`, чистый `PredictEngine`): на фид держим anchor = он-чейн цена
  последнего пуша; return = (binance_mid − anchor)/anchor. ARM при |return| ≥ `KT_PREDICT_ARM_PCT`
  (0.45%, чуть ниже 0.5% для лида), DISARM на ретрейс < `KT_PREDICT_DISARM_PCT` (0.35%, гистерезис)
  без пуша. На подтверждённом пуше anchor := текущий binance_mid (return≈0). Bootstrap anchor из
  он-чейн `latestRoundData` (агрегаторы читаются ПРЯМЫМ eth_call — access-control рубит multicall-
  вызов от контракта). Пуш детектится по смене `updatedAt` (poll `KT_PREDICT_POLL_SEC`=2с).
- **Pre-arm** (`KT_PREDICT_LIVE`, дефолт OFF): на ARM фида F `_arm_candidates` расширяет потолок HF
  для рынков F с `KT_ARM_HF` до `KT_PREDICT_ARM_HF`=1.006 и кап до `KT_PREDICT_ARM_MAX_N`=8 —
  БОЛЬШЕ pre-signed кандидатов (та же evaluate/экономика на цель, меняется только КАКИЕ армим).
  Реакция на реальный пуш схлопывается до insert-tip+broadcast. Пустой набор (shadow/off) →
  `_arm_candidates` байт-в-байт как раньше. Poll агрегаторов идёт через ВЫДЕЛЕННЫЙ коннект
  `_PredictAggReader` (своя http.client-линия, reconnect-once, 3с) — НЕ трогает process-global
  `analysis.rpc._POOL` главного цикла (иначе гонка на не-thread-safe сокете рвала бы блок-поллинг
  армд-окна). Единственные пользователи `_POOL` остаются в однопоточном главном цикле.
- **SHADOW дефолт** (`KT_PREDICT_SHADOW=1`): WS+anchor+return+сигнал считаются и ЛОГируются, но НЕ
  pre-arm/не меняем cadence (только измеряем реальный лид + FP на своём потоке ДО live-pre-arm).

Грамматика shadow-лога (для grep-анализатора; `PREDICT ` + key=value, `-`=нет значения):
  `event=bootstrap` — feed, anchor, source(onchain): стартовый anchor из он-чейн цены.
  `event=arm`       — feed, ret_pct(знаковый %), ret_bps(|bps|), anchor, mid, dir(up|down).
  `event=confirmed` — пуш пришёл ПОКА ARMED: feed, was_armed=1, lead_s(=push_ts−arm_ts, лид!),
                      ret_pct, arm_ret_pct, anchor, push_mid.
  `event=push`      — пуш БЕЗ активного arm (recall-мисс/суб-порог): feed, was_armed=0, lead_s=-.
  `event=disarm`    — armed→ретрейс ниже гистерезис-полосы без пуша (FP): feed, held_s, ret_pct,
                      peak_ret_pct.
  `event=falsepos`  — armed, держали ДЕВИИРОВАННЫМ дольше `KT_PREDICT_HOLD_SEC`=600с без пуша/
                      ретрейса (реальное расхождение Binance-vs-медиана): feed, held_s, ret_pct,
                      peak_ret_pct. АРМ НЕ снимается по таймеру — только по ретрейсу (disarm) или
                      этому cap'у; 600 > lead p90 (132с BTC/325с ETH), 90с рвал slow-build TP.
  live-only: `event=prearm`(feed,markets,n) / `prearm_clear`(feed) — открыли/закрыли широкий сет.
Анализ: FP-rate = (disarm+falsepos)/arm; recall = confirmed/(confirmed+push); распределение лида —
lead_s по `confirmed`. Всё меряется на НАШЕМ live-потоке (валидирует ресёрч ~30-40с/~46% ДО live).
ЧЕСТНЫЕ ОГОВОРКИ: (1) pre-arm НИКОГДА сам не шлёт — fire только mempool/fast-path на реальном пуше;
(2) Binance single-venue — прокси node-медианы Chainlink, отсюда ~46% FP (гистерезис их гасит, а
не firing); (3) WS/RPC down = деградация до текущего поведения, не wedge. `KT_PREDICT` unset = бот
как сегодня (ни потоков, ни поллов, ни изменения arm).

Тесты: 239/239 зелёные (было 202: +9 pricefeed, +21 predict, +7 executor-prearm/poll-изоляция).
Экономика/гарды/sizing/kill-switch/fire-логика/Phase-2 bid — БАЙТ-в-БАЙТ, это только детект/
расписание.

## 2026-07-18 — фиксы ревью (A: гонки нонса, B: Sushi Partial, C: shadow-widen, D: stale mid)

- **A (обязательно ДО KT_MEMPOOL_LIVE=1):** ВСЕ мутации `st["fires"]/["gas_usd"]/["sent"]` — под
  `_fire_lock` (fire(), refund в _fire_fast, _record/_settle/_record_send_error/_post_broadcast/
  _check_pending); WSS-поток клеймит нонс через равенство `fires_at_sign == st["fires"]` под этим
  же локом. `_arm_refresh` читает счётчик под локом ДО nonce-RPC/подписи (fire в середине арма
  теперь ИНВАЛИДИРУЕТ entry — безопасное направление). `save_state` сериализует st под локом
  (файл пишется вне) — раньше json.dump мог упасть на «dictionary changed size during iteration».
- **B (живая деградация fast path):** Sushi status=Partial (роут покрывает ЧАСТЬ amount; для
  weETH/vbETH/avKAT на крупных размерах — постоянный) теперь `PartialRouteError`: fail-fast, без
  ретраев, частичный output НИКОГДА не считается полным. evaluate() кэширует минимальный
  Partial-размер per (coll,loan) на DECLINE_TTL и скипает заведомо-большие фракции БЕЗ сети —
  лестница фракций доходит до проходного размера внутри arm-дедлайна. Budget-stop в _arm_refresh
  (обрыв ниже одного quote) больше не классифицируется как экономический decline (60с-самобан
  замораживал лестницу). Armed-entry для Partial-тяжёлой пары строится за ~2 окна (тест).
- **C (`KT_PREDICT_SHADOW_WIDEN`, дефолт 1):** в SHADOW предикт теперь ТОЖЕ расширяет arm-set
  (строится/подписывается как в live) — иначе все MEMPOOL signal шли с n_armed=0 и решение о
  KT_MEMPOOL_LIVE не на чем принимать. Броадкаста нет ПО ПОСТРОЕНИЮ: `_mempool_signal` шлёт
  armed-entry в `_shadow_same_block` (только лог, send-вызова нет) если не `_same_block_live()`
  (требует MEMPOOL_LIVE=1 + SHADOW=0 + live executor). Газ/kill-switch за shadow-arm не трогаются.
  `prearm`/`prearm_clear` несут `mode=live|shadow_widen`; на старте — громкая строка.
- **D (`KT_PRICEFEED_STALE_SEC`, дефолт 10с):** `PriceFeed.mid()` → `(mid, ts)`; драйвер
  замораживает движок фида при mid старше порога (обрыв Binance WS, бэкофф до 30с): никаких
  arm/disarm/falsepos из стухших периодов, пуши откладываются до recovery (ре-анкор по СВЕЖЕМУ
  mid), `PREDICT event=stale`/`recovered` один раз на эпизод, arm-таймер сдвигается на длину
  слепого окна (falsepos зреет только по НАБЛЮДАЕМОМУ времени, lead_s не пачкается).

Тесты: 270/270 зелёные (+11 A-гонки, +10 B-Partial, +4 C-widen, +6 D-stale). Экономические
гарантии не тронуты: Partial никогда не считается полным филлом, shadow не шлёт и не начисляет.
