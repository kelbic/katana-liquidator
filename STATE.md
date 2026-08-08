# STATE — Katana Morpho Liquidator

Battle-ready liquidation bot for Morpho Blue on **Katana** (chainId 747474). This file is the
verdict + architecture + economics + decision log. Operator handoff steps are in `README.md`.

## 2026-08-08 — ПЕРВЫЕ ТРИ БОЕВЫХ ВЫСТРЕЛА, И ВСЕ ТРИ В МИНУС (commit 460afed)

**Факт.** 08.08 бот сделал первые ликвидации за всю свою историю — три штуки, 02:40 / 05:18 /
06:02 UTC, все успешные on-chain (status 0x1), и все убыточные:

| tx | блок | заёмщик | излишек | газ | итог |
|---|---|---|---|---|---|
| `0xe824764e` | 39414047 | `0xe5f9d865…` | 0.000616 dUSD | $0.1872 | **−$0.187** |
| `0x168b1ccb` | 39423524 | `0x0b530e05…` | 0.000201 dUSD | $0.1829 | **−$0.183** |
| `0xa777d197` | 39426143 | `0xc5f1c489…` | 0.0000245 dUSD | $0.1829 | **−$0.183** |

Итого **−$0.55**. Потолок потерь держал не пол прибыли, а суточный газ-гард
(`KT_MAX_DAILY_GAS_USD=10` ⇒ ~54 таких выстрела в сутки максимум) и он же — единственное,
что стояло между нами и медленным сливом. On-chain `minProfit` тоже держал: **в единицах
займа** каждая tx была плюсовой, минус целиком в газе, которого гейт не видел.

**Причина — цепочка fail-open, три слоя, все распускаются от ОДНОГО незнания.** Рынок
yvvbUSDT/dUSD (`0x65b7a881…`) авто-обнаружен, в жёстком `MARKETS` его нет, а токен займа
dUSD (dTRINITY USD, `0xca52d087…`, 18 dec) не заведён в `_loan_usd_px`. Дальше в `evaluate()`:
1. `usd_floor_wei = … if loan_px else 1` — порог $20 стал **1 wei**;
2. `net_usd = … if loan_px else None` — и потому `net_usd >= MIN_PROFIT_USD`,
   **ЕДИНСТВЕННАЯ проверка, где вычитается газ**, просто не выполнилась;
3. осталось условие «любой положительный `net_wei`» — бот честно выстрелил по пыли.

Комментарий на `_loan_usd_px` этот же дефект уже описывал («без этого min profit
вырождается в 1 wei») и был **закрыт только для weETH/vbETH** заведением `ETH_USD`; общий
случай «токена нет в реестре» остался открытым и дождался своего рынка.

**Правка.** Ранний отказ в `evaluate()` ДО первой квоты: `not loan_px or loan_px != loan_px`
(None / 0.0 / NaN). Вывести цену не из чего — квота отдаёт единицы займа, не доллары —
значит корректный ответ ровно один: не стрелять. Пропуск печатается **именованно**
(`skip: НЕЦЕНЕНЫЙ заём 0x…`), не под маской «no profitable chunk» (чанк может быть каким
угодно жирным, мы просто не умеем его оценить), и дедуплится раз в `DECLINE_TTL` на токен.
Закрыт **ровно выстрел**: ранжирование (`_prize_usd`), гейт `MIN_DEBT` и алерты гонок
фейлятся открыто НАМЕРЕННО и не тронуты; `FEE_BID` уже фейлился закрыто.

**Почему fail-closed безопасен** (проверено, а не предположено): `ETH_USD`/`KAT_USD` имеют
ненулевой seed из env и при отказе refresh **сохраняют прежнее значение** (перезапись только
внутри sanity-полосы), поэтому сетевая икота не может погасить боевой рынок. Пришпилено
тестом `test_every_registry_market_loan_is_priceable`.

**Названный предел закрыт замером, а не сноской.** Из 30 near-edge рынков в живой книге
нецененных ровно два, оба пустые:
- `dUSD` — тот самый, суммарный долг рынка **6.58 dUSD** (весь приз рынка ≈ $0.29, он
  физически не способен окупить $0.19 газа даже целиком);
- `vbWBTC`-как-заём (`0x0913da6d…`) — долг **0.00**, мёртвый.

**Известная дыра, оставлена сознательно:** `_loan_usd_px` не знает BTC-номинированных займов
(vbWBTC/LBTC), хотя `KT_BTC_USD` в env есть и `monitor._APPROX_USD` им пользуется. Сейчас
это стоит ноль (рынок пуст). Триггер завести: **если BTC-номинированный рынок наберёт долг**
— и заводить не статический seed, а `refresh_btc_usd()` по образцу `refresh_eth_usd`, иначе
дрейф цены перекосит пересчёт $20-порога в wei.

**Приёмка — на боевых числах, с обоими контролями.** Фикстура собрана из настоящей
`tx 0xe824764e`: `seized`/`repaid`/`shares`/`price` из события Morpho `Liquidate`, выход
свопа и курс `redeem` — из её же `Transfer`-ов.
- **отрицательный контроль:** гард временно снят — фикстура воспроизводит боевой выстрел
  бит-в-бит (`f=1.0`, `net_usd=None`, `min_profit_wei = net_wei//2`), 3 теста краснеют;
- **с гардом:** `None` и **ноль вызовов квоты** — гард стоит выше сети, а не после неё;
- **позитивный контроль НА ТОМ ЖЕ объекте:** подменён только адрес займа на прайсуемый
  18-значный vbETH — лестница проходит 8 ступеней и отказывает по честной экономике, а с
  жирным выходом стреляет ⇒ `None` выше не артефакт фикстуры;
- то же прогнано против **развёрнутого** файла (не только против импорта из репо).
343 теста зелёные. Бот перезапущен по точному PID из `bot.pid.txt` (крон поднял за 55с),
полные проходы идут, новых выстрелов нет.

**Откат:** `git revert 460afed && kill $(cat ~/.katana-bot/bot.pid.txt)` — крон поднимет.

### Побочно: ЧЕСТНЫЙ ЗНАМЕНАТЕЛЬ КАТАНЫ — весь поток протокола за 23 суток $11.39 бонуса

Вопрос «мы хотя бы выиграли эти ликвидации?» разобран замером цепи, а не по логу бота.
Скан `Liquidate` по Morpho Katana, 2 000 000 блоков (~23 суток, блоки 37450549..39450549):

| | |
|---|---|
| ликвидаций во ВСЁМ протоколе | **14** |
| исполнителей | 5 (`0x85593464`×6, наши×3, `0x5d8ecd93`×2, `0xe0557296`×2, `0x32156dff`×1) |
| суммарный repaid | **$127.03** |
| суммарный БОНУС, доступный ВСЕМ ликвидаторам вместе | **$11.39** |
| самая крупная ликвидация за 23 суток | repaid $39.78 ⇒ бонус **$5.04** |
| наши три | repaid $0.02 |

**Следствие, важнее самого дефекта: НИ ОДНА из 14 ликвидаций за 23 суток не взяла бы наш
пол $20.** Крупнейший возможный приз ($5.04) в четыре раза ниже гейта. То есть бот 26 суток
не стрелял НЕ от слепоты — он был прав, а три выстрела 08.08 состоялись ровно потому, что
дефект снял пол. За последние 200k блоков (~2.3 суток) во всём протоколе было 3 ликвидации,
и все три наши — «выиграли» верно буквально, но гонки не было ни одной (`RACE`-телеметрия
за всю историю лога: **0** чужих событий). Победа здесь не доказывает скорость.

**Чем Katana остаётся ценна — это ОПЦИОН на стресс, а не поток.** Добыча существует, но
не пересекает: hot-set держит призы $34.8k (stcUSD/vbUSDC, HF 1.0443), $32.4k (weETH/vbETH,
HF 1.0165), $23.1k (`0x6691cdca`, HF 1.0072) при книге $51M avKAT/KAT + $14M vbWBTC/vbUSDC.
Замер 30.07 объясняет почему: ни один рынок Katana не таймер — коллатерал дорожает быстрее,
чем капает заём, дрейф HF **+0.0027/сутки ВВЕРХ**, позиции у края самозалечиваются. Вниз их
толкает только собственный до-заём владельца (потому и стоит вотчер `Borrow`) либо ценовой
шок. Стоимость удержания опциона: газа ноль (мы не стреляем), цена — RPC и ядро VPS.

**Калибровка газа с боя (первые настоящие числа вместо форковых):** vault-путь съел
**1,904,505** газа против форковой оценки 1,692,244 — **+12.5%**. `KT_GAS_LIMIT=2,600,000`
оставляет запас 27%, менять не нужно, но форковую оценку впредь считать нижней границей.

**Что эти три выстрела доказали положительно:** контракт v2 (`0xd8ADeE48`) впервые отработал
в БОЮ по полному vault-пути — `previewRedeem`-сайзинг → `redeem` yvvbUSDT→vbUSDT → своп →
repay → сметание излишка, три раза подряд, `status 0x1`, **ноль ревертов за всю историю
бота**. Раньше этот путь был проверен только на форке; yv-рынки, ради которых v2 и строился
(Sushi отдавала `NoWay` на доли Yearn), теперь доказанно достижимы боевым кодом.

## 2026-07-22 — РЫНКИ 8-9: stcUSD/vbUSDC + avKAT/KAT (discovery-скан, commit d5e8b99)

Полный discovery по Morpho GraphQL (все ~30 живых рынков Katana + near-edge HF≤1.15):
- **stcUSD/vbUSDC** ($808k borrow, near-edge $808k @ minHF 1.019) — депег-опцион capUSD.
  Оракул: stcUSD/capUSD rate + capUSD/USD + наш USDC/USD. Новые фиды — медленные push
  (~4/день, to-матч по агрегатору деградирует: апдейты через сменные батч-контракты; рынок
  ловится обычным сканом, same-block слой некритичен). Sushi 0.12%@$5k / 0.21%@$50k.
- **avKAT/KAT** ($276k, near-edge $137k @ **minHF 1.0013** — у самого края) — vault-only
  оракул (BASE_VAULT=avKAT ERC4626, внешних фидов НЕТ): цена = convertToAssets, «фид» в
  реестре = сам вольт (kind "vault"), армимся на tx к нему. LIF при lltv 0.77 = **7.4%**;
  Sushi 0.64%@$1k / 1.0%@$10k. Ликвидность жидкая — чанк-descent решает.
- **yv-петли НЕ добавлены** (yvvbUSDC/vbUSDT + yvvbUSDT/vbUSDC, $982k near-edge @ 1.033):
  Sushi **NoWay** на Yearn-шары — атомарный выход требует vault.redeem() в контракте.
  Кандидат на контракт v2, отдельное решение.
- **yUSD/vbUSDC — bad debt, мимо**: 20 позиций HF 0.01–0.07, долг $5.6k против $157
  коллатерала СУММАРНО (депег yUSD) — забирать нечего, потому никто и не брал. После
  рестарта бот сам оценил их и скипнул («no profitable chunk») — поведение корректное.

290 тестов зелёные. Рестарт ок: positions 25→562 (API-рефреш по 9 рынкам), hot-poll
снова capped-25, predict-слой поднялся. Латентность не тронута (тот же профиль, что
weETH/vbUSDT: роут тянется только для HF<1 целей).

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

---

## 2026-07-20 — ЧАНКИНГ (d6a70a1), ФАЗА 2 (бэктест 254a652), v5 LIVE

### Чанкинг: узкое место было НЕ в маршруте и НЕ в ставке, а в нарезке

`CHUNK_FRACTIONS` резала позицию долями ОТ ПОЛНОГО ЗАКРЫТИЯ с полом `3/50` = 6%. Но пул
ограничивает АБСОЛЮТНЫЙ объём, а не долю: у кита `0x14bcd9da05…` (~$16M долга, ~196 vbWBTC)
6% = ~10.6 vbWBTC, всё ещё ~30× глубины пула. `evaluate()` проходила всю лесенку, каждая
ступень Partial, и возвращала None. Измеренная кривая (блок 16693558): 176 vbWBTC →
подразумеваемый BTC $657; 10.59 (пол лесенки) → $10,916; 0.353 → **$85,548** (истинная цена).
Конкуренты берут мелкими АБСОЛЮТНЫМИ кусками — tx `0x1ca415902d86…` содержит ЧЕТЫРЕ
`Liquidate`-лога по одному заёмщику в одной транзакции (проверено по рецепту).

**Фикс — экономически ограниченный геометрический спуск** (`_chunk_fractions`, executor.py
:358-421). Ниже пола лесенки доля делится пополам (`3/100, 3/200, …`) до ДВУХ границ,
считаемых БЕЗ сети: `f_min = (MIN_PROFIT_USD + gas_usd) / full_prize_usd` (net линеен по f:
бонус масштабируется, газ нет ⇒ ниже f_min не пройдёт НИЧТО) и жёсткий `MIN_CHUNK_FRACTION`
= 0.0002 (`KT_MIN_CHUNK_FRACTION`, он же гарантия завершения). Быстрый путь не тронут: последова-
тельность ленивая, `evaluate()` рвётся на первом прибыльном чанке ⇒ обычная позиция не
порождает ни одной ступени спуска. Бисекция рассмотрена и ОТВЕРГНУТА: монотонность Partial её
допускает, но ей нужен bracket, а поиск bracket И ЕСТЬ этот спуск — суммарно больше
round-trip'ов ради точности лучше 2×. Монотонность используется дёшево: `_partial_known`
делает бесплатным пропуск любой ступени ≥ известного Partial.

**Результат (мой независимый прогон `analysis/phase2_backtest.py` на чистом кэше, сверен со
значениями агента — совпало до тикета):**

| | ДО | ПОСЛЕ |
|---|---:|---:|
| маршрут собрался | 36/95 | **63/95** |
| кит `0x14bcd9da05…` | 5/64 | **32/64** |
| прочие заёмщики | 31/31 | 31/31 |
| суммарный net | $93,853 | **$112,875** |
| тикетов ≥$300 | 35 | **44** |

Восстановлено 27 тикетов на $19,022 (медиана $94). Регресс-доказательство: **36/36 старых
строк не изменились** ни долей, ни центом ⇒ быстрый путь не задет. Макс impact 1.92% (кап 2%).
Тесты 276 → 284, все 12 модулей зелёные.

### Фаза 2 (KT_FEE_BID, ~0.6 ETH): решение — СНАЧАЛА ЧАНКИНГ, он уже сделан

Бэктест 95 исторических тикетов ≥$300 (вся история Morpho Katana: 406 суток, 625 ликвидаций,
395 в наших 6 рынках, призовой пул $221k). Метод: узел отдаёт АРХИВ, все пулы маршрута —
Uniswap-V3, `eth_call` со stateOverride ⇒ симуляция свопов на исторических tick-состояниях.
Сверка с живым Sushi API: отношение 0.9990–1.0000, всегда ≤1 ⇒ измерение, слегка
консервативное, НЕ верхняя граница.

- **Видимость никогда не была узким местом**: 95/95 уже были в hot-set (86/95 с HF<1 на blk−1;
  остальные 9 пересекли границу ВНУТРИ блока ликвидации — ровно то, для чего v5/fastpath).
- **Ставка покупает не всё**: 27 восстановленных чанкингом тикетов ставка НЕ КУПИТ ни за какие
  деньги — это дефект кода, а не цены.
- **Методологическая честность**: на 35/36 тикетов наша ставка перекрыла бы наблюдаемую, но это
  НЕ засчитано как победа — наблюдаемая ставка есть цена В НАШЕ ОТСУТСТВИЕ, а не цена
  вытеснения; при нашем участии лучший ответ конкурента сдвигается. Всё меряет «смогли бы
  участвовать», не «выиграли бы».
- **Осторожно с двухнедельным окном**: замер 19.07 за 14 суток дал 0 ликвидаций в наших рынках,
  0 из 18 блоков со следами борьбы и ставки победителей 0.0000-0.0001 gwei — и я ошибочно счёл
  предпосылку Фазы 2 фальсифицированной. Полная история опровергла: поток ЭПИЗОДИЧЕСКИЙ
  (ноябрь 2025 — 183 ликвидации в наших рынках, февраль 56, январь 43, июль 0), а на крупных
  тикетах аукцион РЕАЛЕН: медиана 0.075 gwei, p90 94.8, max 557.7 — диапазон 171-443 внутри.
  УРОК: короткое окно на эпизодическом потоке измеряет затишье, а не режим.

### v5 (`KT_PREDICT_LIVE=1`, SHADOW=0) — ВКЛЮЧЁН live 19.07 по решению kelbic

Слой САМ НЕ ШЛЁТ: только расширяет pre-signed arm-set (HF до 1.006, кап 8) для рынков
движущегося фида. Реальный выстрел — классический путь; `KT_MEMPOOL_LIVE=0` (same-block
остаётся в shadow). Валидация 24.9ч, покрытие 100%: lead медиана **46с** (p25 31, p90 221,
min 9), FP **44%**, recall **89%** — все три не хуже ресёрча (30-40с / ~46% / ~72%). Recall
СВЕРЕН ПО ЧЕЙНУ (`analysis/predict_analyze.py`): все `AnswerUpdated` агрегаторов BTC/USD и
ETH/USD за окно — on-chain 28 против 25 confirmed + 3 push = 28 в логе, пропущенных нет.

Первый боевой цикл: оба фида ушли на ~0.49%, `prearm … n=4 mode=live`, подтверждение через
29с, **0 ошибок подписи**. Но `n_armed=0` — подписывать было нечего (в логе рядом
`partial route`). Это ЧЕТВЁРТОЕ независимое подтверждение одного и того же: подготовка
отточена, брать нечего. (Остальные три: 0 ликвидаций в наших рынках за 14 суток;
`shadow_fire`=0 при 60 сигналах; `armed>0` в 0 из 60.)

Безопасность проверена ДО включения: `fires_at_sign` читается под `_fire_lock` ДО nonce-RPC и
подписи (внешний выстрел ⇒ заготовка отбраковывается, двойного nonce быть не может); ошибка
подписи ⇒ `arm sign failed (classic path covers)`; газ списывается ТОЛЬКО вместе с
`st["fires"] += 1`, то есть при реальной отправке — взвод kill-switch не кормит.
ОТКАТ: `KT_PREDICT_SHADOW=1 KT_PREDICT_LIVE=0` + рестарт.

**Операционное правило:** пока v5 боевой, НЕ гонять канарейки с боевого кошелька на Katana —
предварительная подпись вмораживает nonce, внешняя tx делает заготовку мусором. Деградация
мягкая (send_error → пересборка классическим путём, деньги не горят), но фора теряется.
Проверено: Katana и WC делят адрес `0x3E8E4B5EB633F5e3CdC5657A3BD16f01c080C4D5`, однако nonce
ведётся ПО ЦЕПЯМ отдельно (Katana 5 / WC 9) ⇒ выстрелы WC заготовки Katana не ломают.

### Фаза 2 — ПЕРЕ-ПРОГНАНА на исправленной нарезке (20.07). Решение: ЖДЁМ РЕАЛЬНОГО СОБЫТИЯ

| | до починки нарезки | после |
|---|---:|---:|
| тикетов с маршрутом | 36/95 | 63/95 |
| из них ≥$300 (порог Фазы 2) | 35 | **44** |
| осталось бы после ставки | $54,106 | **$60,581** |
| медиана остатка | $433 | $247 |

Прирост под ставку скромный (+9 тикетов, +$6,475): нарезка вернула в основном МЕЛКИЕ тикеты
(медиана $94), а Фаза 2 работает только от $300. Два инструмента бьют по РАЗНЫМ сегментам —
починка нарезки Фазу 2 почти не усиливает, и наоборот: 27 возвращённых нарезкой тикетов ставка
не купила бы ни за какие деньги.

**Главное число для решения — доля побед для безубытка ≈ 41%** (медиана по 44 тикетам; худший
требует 75%, лучший 1%). Частота: 1 тикет на 9.2 суток. Потеря при проигранной ставке —
медиана $163 (наша же ставка × ~145k газа реверта). EV за 406 суток:

| доля побед | EV | в год |
|---:|---:|---:|
| 20% | $7,304 | +$6,566 |
| 30% | $13,963 | +$12,553 |
| **41% (безубыток)** | ~$21,000 | ~$19,000 |
| 50% | $27,283 | +$24,528 |
| 70% | $40,602 | +$36,502 |

Капитал 0.6 ETH ≈ $1,127 НЕ тратится — он лежит конвертом комиссии (узел отвергает бид, если
баланс < GAS_LIMIT × maxFeePerGas). Риск — сожжённый газ на проигранных ставках.

**ПОЧЕМУ НЕ ФОНДИРУЕМ СЕЙЧАС (два довода, оба структурные):**

1. **Долю побед мы не знаем и не можем узнать из истории.** Наблюдаемая ставка победителя есть
   цена В НАШЕ ОТСУТСТВИЕ, а не цена вытеснения: появимся мы — его лучший ответ сдвинется. Формально
   наша доступная ставка ≥ наблюдаемой на 44/44, но это «смогли бы участвовать», НЕ «выиграли бы».
   Единственный способ узнать — первые реальные ставки малым шагом с записью реакции.
2. **Медиана НАШЕЙ ставки = 600 gwei, то есть уже `KT_MAX_PRIORITY_GWEI`.** Мы упирались бы в
   потолок почти всегда ⇒ запаса на эскалацию нет; при ответной ставке конкурента мы просто
   проигрываем и платим за реверт.

Плюс контекст: поток ЭПИЗОДИЧЕСКИЙ, и сейчас пауза — 0 ликвидаций в наших рынках за 14 суток,
`shadow_fire`=0, `n_armed`=0 на первом боевом взводе v5. Фондировать аукцион, которого в текущем
режиме не происходит, преждевременно: 0.6 ETH пролежат без работы, а решение всё равно упрётся в
неизмеримую долю побед.

**ТРИГГЕР ПЕРЕСМОТРА — реальное событие, а не новый расчёт.** Возвращаемся к вопросу, когда
одновременно: (а) в наших рынках появляются тикеты ≥$300 (хотя бы несколько в неделю);
(б) в блоках с ликвидациями видны провальные попытки конкурентов (признак борьбы);
(в) ставки победителей поднимаются выше нашего дефолта 0.001 gwei.
Инструмент для (а)-(в) — RACE-телеметрия; ей не хватает ДВУХ полей: ставка победителя и пометка
contested/uncontested. Дёшево, капитала не требует.

### Что дальше (не сделано, ждёт решения)

1. ~~Пере-прогнать Фазу 2 после чанкинга~~ — СДЕЛАНО 20.07, см. раздел выше: 44 тикета вместо
   35, безубыток на 41% побед, решение — ждать реального события.
2. **Батчинг слайсов**: конкурент кладёт 4 `Liquidate` в одну tx. Мы после фикса берём один
   кусок за выстрел — на ките это оставляет деньги на столе (32/64, а не 64/64).
3. **Покрытие рынков**: 15 из 19 ликвидаций последних 14 суток — в `wsrUSD/vbUSDC`, которого у
   нас НЕТ. Дёшево добавить, но призы там пылевые ($0.28 медиана) — сначала померить.
   ~~weETH/vbUSDT~~ — ДОБАВЛЕН 21.07 (см. секцию ниже).

## 2026-07-21 — 7-Й РЫНОК weETH/vbUSDT (стресс-опцион) + self-flock (commit d102f16)

**Контекст (запрос kelbic):** «глянем ближайших здоровых кандидатов, и волатильно-зависимых».
Скан near-edge (95 здоровых, HF≥1, долг≥$50) показал структурную картину: у грани стоят большие
деньги, но почти все **не двигаются от волатильности** — либо коррелированные лупы (weETH/vbETH,
оракул=обменный курс weETH_FUNDAMENTAL; wstETH/vbETH), либо стейбл-йелд-лупы (yvvbUSDC→vbUSDT
$1.26M, yvvbUSDT→vbUSDC $820k, stcUSD/siUSD/yvAUSD — депег-риск, не волатильность, и почти все
**no-route** yield-vault обёртки). В наших покрытых волатильных рынках (vbETH/vbUSD*, vbWBTC/vbUSD*,
LBTC/vbUSDC) near-edge позиций — **0**.

**ЕДИНСТВЕННАЯ волатильно-зависимая позиция у грани:** `weETH → vbUSDT` — **$645k, HF 1.109**
(нужна просадка ETH ≈ **−9.8%**), weETH-коллатерал против USD-долга ⇒ HF реально ходит от цены ETH.
Была ВНЕ покрытия — тот же gap, что нашли 15.07 (тогда не добавили: в непокрытых рынках нет
исторического потока ликвидаций). **Пересмотрено по просьбе kelbic — добавлен как опцион на крах.**

**Латентность НЕ затронута (условие kelbic).** Добавление рынка не создаёт нового механизма на
fire-пути: (1) роут берётся из Sushi API только когда позиция уже цель (HF<1) — ровно как у всех 6
рынков; (2) hot-poll капнут на 25 позиций, weETH/vbUSDT войдёт туда лишь при HF<1.02 (после ~−10%
ETH), вытеснив меньшую по долгу; (3) рост стоимости скана — на слоу-пути (API 30с), не на гонке.

**Верифицировано ВЖИВУЮ до правки (не на глаз):**
- роут Sushi `weETH→vbUSDT` валиден и прибылен: impact **0.23% @$5k**, 0.83% @$25k, 1.6% @$100k,
  3.0% @$250k — < LIF 4.4% примерно до **$250k/чанк** (дальше чанк-дескент режет кита);
- market params из Morpho on-chain: `id 0xbb4fb94c…`, `oracle 0xE8926ab…`, `lltv 0.86`; токены
  weETH+vbUSDT уже были в `TOKENS`;
- feed-декомпозиция сверена по оракулу on-chain: `BASE_FEED_1=weETH_FUNDAMENTAL`,
  `BASE_FEED_2→ETH/USD` ⇒ feeds `["ETH/USD","weETH_FUNDAMENTAL"]`. Теперь ETH/USD-пуш пре-армит и
  этот рынок (predict-слой работает на него; ETH/USD→3 рынка, weETH_FUNDAMENTAL→2).

**Правки:** `analysis/protocols.py` MARKETS (+запись), `bot/oracles.py` `_MARKET_FEEDS_BY_PAIR`
(+feeds). Поле `route` НЕ нужно — маршрут динамический в `bot/sushi.py`. 3 теста обновлены под
+1 рынок (7 рынков), **240 зелёных**. Katana перезапущена на новом коде (7 рынков, guard=OK, 0
ошибок). Также добавлен **self-flock guard в `~/.katana-bot/run.sh`** (fd 9 на
`/tmp/katana-executor.self.lock`) — та же защита одиночности, что для wc 21.07 (sandbox-flock
внешнего cron ненадёжен, плодит дубли при рестарте).

**Как добавить рынок Katana (памятка):** в `analysis/protocols.py` MARKETS — `{id, lltv, loan,
coll, oracle}` (loan/coll = ключи в TOKENS; oracle = Morpho market oracle, checksummed); в
`bot/oracles.py` `_MARKET_FEEDS_BY_PAIR` — фиды по декомпозиции оракула (base-волатильный первым).
Route не задаётся. Проверить: `sushi.quote_for_seized(coll,loan,seized,contract)` не NoRoute +
impact < LIF; тесты в `test_oracles.py` хардкодят число рынков и наборы feed→market — обновить.

**Состояние:** weETH/vbUSDT здорова (HF 1.11), сидит тихо. При просадке ETH ~10% бот увидит её у
грани, оценит роут и возьмёт прибыльным чанком. Опцион занят, латентность не тронута, ждём стресс.

## 2026-07-27 — разбор 6 «целей» под линией: `no profitable chunk` ВЕРЕН, дело не в маршруте

Бот месяцами показывает `targets(HF<1) 6` в API-проходе и ни одного выстрела (`fires 0`
при 521,981 проходе). Рабочая версия была «жертвы есть, но нет маршрута выхода»
(в логе рядом висит `quote partial: no full-fill route at this size`). **Версия
неверна.** Разбор всех шести целей read-only (`scan()` + чтение токенов):

| # | заёмщик | HF | залог → долг | долг | остаток залога | приз |
|---|---|---|---|---|---|---|
| 1 | `0x499cfcad…` | 0.0127 | **yUSD** → vbUSDC | $733.47 | 10.878 yUSD ≈ **$10.9** | **$0.456** |
| 2 | `0x92328ffc…` | 0.0159 | **yUSD** → vbUSDC | $586.75 | 10.888 yUSD ≈ **$10.9** | **$0.457** |
| 3 | `0x83a9f3bd…` | 0.3661 | LBTC → AUSD | $0.00 | **4 wei** | $0.0001 |
| 4 | `0x310aa34c…` | 0.6244 | vbWBTC → AUSD | $0.00 | **13 wei** | $0.0004 |
| 5 | `0x536ad066…` | 0.6195 | vbWBTC → AUSD | $0.00 | **18 wei** | $0.0005 |
| 6 | `0x552c87b8…` | 0.9032 | BTC.b → AUSD | $0.00 | **7 wei** | $0.0002 |

`yUSD` = YieldFi `0x4772d2e014f9fc3a820c444e3313968e9a5c8121` (18 dec).
LIF при lltv 0.86 = `1/(1 − 0.3·(1−0.86))` = **1.04384** ⇒ приз = 4.384% от repaid.

**Суммарный теоретический приз по всем шести: $0.91** — при идеальном исполнении и
нулевом газе. Отклонение бота корректно; чинить нечего.

### Урок (общефлотский, продублирован в edge-research)

yUSD **уже депегнулся**, залог уценён на **98.5%** ($10.9 против $733 долга) — и весь
эпизод заплатил 91 цент. Причина структурная: ликвидатор берёт процент **от того, что
осталось**. Прибыльное окно депега узкое — залог должен пробить LLTV, но остаться выше
долга; жёсткая уценка даёт безнадёжный долг, а не приз.

Отсюда: **нотионал книги ≠ стоимость депег-опциона.** Тезис «мы бесплатно держим ~$300M
депег-опционов на непокрытых чейнах» — неверная бухгалтерия, и сортировать венью по
объёму заимствований бессмысленно. Правильный скрин — самая большая **одиночная** позиция
у линии, а не сумма книги.

### Побочное следствие для этого бота

Четыре из шести целей — пыль в единицы wei, две — безнадёжный долг. Все шесть вечно
сидят в `declined`-кэше и на каждом API-проходе стоят одну запись в лог. Это не баг
(dedup-кэш ровно для того и сделан, RPC они не жгут), но и `targets(HF<1) 6` в статусной
строке **не следует читать как «шесть жертв ждут»** — это шесть мертвецов. Живой сигнал
для katana — по-прежнему near-edge на рынках из `MARKETS`, прежде всего avKAT/KAT
(HF 1.0013 на 22.07) и stcUSD/vbUSDC.

---

## 🔫 ПЕРВЫЙ БОЕВОЙ ВЫСТРЕЛ (2026-07-29, блок 38600666)

После **609 256 проходов** и нуля выстрелов за всю жизнь бот отработал цель — успешно.

`0x232000d2dcd9a7138e151237945144784d203ed5b829bef2162926e63b985a34` — status `0x1`,
заёмщик `0xfeed46c11f57b7126a773eec6ae9ca7ae1c03c9a`, рынок
`0xca087cacb962ab9c…`, HF=0.9975, chunk=100%.

### Деньги (расписка, не оценка)

| | |
|---|---|
| repaid | **2.024246 AUSD** |
| seized | **0.00003307 LBTC** |
| маршрут | LBTC → BTC.b (UniV3 `0x6205751b…`) → AUSD (UniV3 `0x1ddc6d10…`), Sushi RP `0xc10ee903…` |
| выручка свопа | 2.111150 AUSD |
| **излишек на контракте** | **0.086904 AUSD** → выметен на EOA, контракт чист (0 AUSD / 0 ETH) |
| газ | 472 893 × 0.051 gwei = 2.4118e-5 ETH = **$0.0455** |
| **ЧИСТО** | **+$0.0414** |

Разложение приза: теоретический LIF при lltv 0.86 = 4.384% ⇒ максимум **$0.0887**.
Маршрут съел **$0.0018** (2.1% приза; бот оценивал impact −0.10% — сошлось).
**Газ съел 52% излишка.** На позиции в $2 газ — доминирующая статья, а не проскальзывание.

### Что подтвердилось

- **Модель прибыли точна до единицы.** Лог: `net=86905wei`. Факт: **86904**. Ошибка 0.001%.
  Гейт «no profitable chunk» отклоняет по верным числам — шесть вечных «целей» отклонены
  правильно (см. разбор выше), а первая настоящая жертва распознана мгновенно.
- **Контракт выметается досуха.** Ни пыли AUSD, ни ETH не осело.
- **Контеншена не было.** send→включение 6.5с / 9 блоков при 1-секундных блоках —
  медленно, но за позицию в $2 никто не боролся. Латентность здесь пока не узкое место;
  цена вопроса не оправдывает оптимизацию.
- Детект→отправка 2.5с (внутри — round-trip котировки Sushi).

### Дыра в форензике, найдена этим же разбором

`_alert_send` (executor.py:546) печатает в stdout **только** на путях mute / disabled /
ошибки. На успешном пути алерт уходит в TG и **не попадает в `executor.log` вообще**:
`grep -c 🔫` по всем четырём логам = 0, хеша `0x232000…` в логах нет. Единственный
локальный след выстрела — `exec_state.json` (`fires: 1`, `sent{}`), который перетирается.

Следствие: **P&L katana из логов восстановить нельзя**, в отличие от wc (там `✅ WIN … +$N net`
пишется в лог). Плюс сам алерт `✅ ok: 0x…` не несёт денег — по нему не видно, заработали
мы или сожгли газ.

Предложение (не внедрено, ждёт «го»): `_alert_send` печатает в лог на **всех** путях;
`_settle` добавляет в алерт исхода излишек, газ и чистыми. Это ~5 строк и закрывает
и лог, и TG разом. Урок общефлотский и того же класса, что
`dead-watchdog-worse-than-none`: **прибор, молчащий на успехе, лжёт умолчанием о
единственном событии, ради которого он существует.**

### ⚠️ Найдено при починке: пол прибыли $20 на этом рынке НЕ ДЕЙСТВОВАЛ

Стартовая строка бота обещает `min_profit $20.0`. На рынке первого выстрела он был **1 wei**,
а газ в решение не входил вовсе.

Цепочка (`bot/executor.py`):

1. `_loan_usd_px` (:605) даёт цену только для `STABLES` и `{vbETH, weETH}`; для всего
   остального — `None`.
2. `usd_floor_wei = … if loan_px else 1` (:799) ⇒ при `None` порог вырождается в **1 wei**.
3. `net_usd = (…) − gas_usd if loan_px else None` — **газ входит в решение только через
   `net_usd`**. При `None` проверка `net_usd >= MIN_PROFIT_USD` просто пропускается (:882),
   и остаётся `net_wei >= 1`.
4. Второй слой тоже слеп: `min_profit_wei = max(usd_floor_wei, net_wei // 2)` (:895).

Рынок выстрела `0xca087cac…` **отсутствует в `MARKETS`** — книга приходит из индексатора
Morpho (564 позиции против 9 рынков реестра), а заём **AUSD** `0x00000000efe302be…`
не значится ни в `TOKENS`, ни в `STABLES`. Отсюда `loan_px = None` и весь каскад выше.

**Насколько близко было к убытку:** ончейн-пол гарантировал `86905 // 2 = 43452` units =
**$0.0435** при фактическом газе **$0.0455**. То есть оба гейта пропустили бы сделку,
закончившуюся в минус; спасло то, что реализовался полный излишек $0.0869, а не пол.
Прибыль +$0.0414 — следствие дешёвого газа (0.051 gwei), а не работы защиты.

**Охват:** все API-рынки с заёмом вне реестра из 9 токенов + `avKAT/KAT` (заём KAT
не прайсуется). Восемь рынков реестра из девяти защищены корректно.

**Не чинил — это меняет поведение бота, а не отчётность.** Развилка для владельца:

- **A. Прайсить AUSD** (добавить в `STABLES`, 6 dec) ⇒ пол $20 включается. Но тогда katana
  почти наверняка не выстрелит больше никогда: вся видимая добыча на чейне мерялась в
  центах (разбор шести целей выше, суммарно $0.91).
- **B. Оставить микро-сделки, но чинить слепоту к газу** — считать пол в units через любую
  доступную цену, а при `loan_px = None` требовать хотя бы `net_wei > газ`, а не `> 1`.
  Тогда 4-центовые выстрелы остаются, но убыточные отсекаются.

**B выглядит правильным:** дефект здесь не в размере порога, а в том, что решение о
прибыльности принимается без учёта газа. $20 — вопрос стратегии, «не стрелять в минус» —
вопрос корректности.

### 📊 Откуда взялась добыча: мерялся ЗАПАС, а надо было ПОТОК (правка прежнего вывода)

Разбор шести целей выше верен по фактам, но отвечал не на тот вопрос. Он мерил **запас** —
позиции, СТОЯЩИЕ под HF<1 в книге. Они действительно мертвы: пыль в единицы wei и
безнадёжный долг, их никто не берёт именно поэтому. Но ликвидация — это **событие**, поток
позиций, ПЕРЕСЕКАЮЩИХ линию. Стоячая книга о потоке не говорит ничего.

Скан всех `Liquidate` Morpho (`0xD50F2Dff…`) за 30 суток, весь чейн, все ликвидаторы:

| | |
|---|---|
| событий | **29** |
| погашено | **$680.60** |
| приз `(LIF−1)×repaid` | **$25.92** |
| ликвидаторов | **11** |
| крупнейший одиночный приз за месяц | **$5.04** |

Распределение приза: `wsrUSD/vbUSDC` **$15.61** (15 событий из 29 — 60% всех денег чейна),
`KAT/vbUSDT` $5.05, `KAT/vbUSDC` $2.91, `avKAT/vbUSDT` $1.90, `avKAT/vbUSDC` $0.26,
наш `LBTC/AUSD` $0.09, остальное — нули.

**Ключевое: `wsrUSD/vbUSDC` — не из реестра `MARKETS`.** Главный денежный рынок чейна
никогда не был в наших девяти. Выстрел 29.07 тоже пришёл с внерееcтрового рынка
(`LBTC/AUSD`) — его нашла API-дискавери, реестр не увидел бы ни того, ни другого.
Прежняя рекомендация «живой сигнал — avKAT/KAT и stcUSD/vbUSDC» была получена из реестра
и потому неполна: avKAT/KAT действительно дали 3 события ($2.16), но это шестая часть чейна.

**Наша доля:** $0.087 из $25.92 за месяц — около 0.3%.

**Побочная находка.** Заёмщик, которого мы ликвидировали — `0xfeed46c11f57b7126a773eec…`,
EOA — сам является одним из самых активных ликвидаторов чейна: **7 ликвидаций** на
`wsrUSD/vbUSDC` в этом же окне. Мы взяли просроченную позицию конкурента, который следит
за чужими и проспал собственную.

**Что это меняет для развилки A/B выше:** крупнейший приз за 30 суток = **$5.04**. Порог
`KT_MIN_PROFIT_USD=$20` не пропустил бы НИ ОДНОЙ из 29 ликвидаций чейна. Вариант A
(включить пол $20) = katana не стреляет никогда — теперь это измерено, а не предположено.

## 2026-07-30 — чек ботов-жертв: найден кластер avKAT/KAT (~$1.3k), скрытый от hot-петли

Отправная точка — побочная находка 29.07 (наша жертва сама была ликвидатором). Проверил
гипотезу «боты-конкуренты, держащие займы, — это готовый watchlist добычи»: взял все
20 адресов поля из 30д-скана (11 ликвидаторов-callers + 9 операторских EOA за ними),
поднял их позиции через Morpho API и пересчитал HF он-чейн.

**Прямой ответ по ботам:** займы держат только двое. `0xfeed46c11f…` (наша жертва) — 9
позиций, все пылевые, минимальный HF 1.126, суммарный приз копейки. `0xe29b7303…`
(ликвидатор-EOA, 1 тейк) — левередж-луп **avKAT/KAT, HF=1.0137, долг 482,610 KAT
(~$2,180), net при закрытии ≈ $82**. Остальные 18 адресов займов не имеют — как watchlist
поле себя не оправдало, но привело к рынку, где лежат настоящие деньги.

### Кластер avKAT/KAT — 7 позиций у самой границы

`scan()` боевым кодом (блок 38673600): 560 позиций, 386 под HF<1.05. Из них 7 — один
рынок `0x80e60fe4…` (avKAT/KAT, lltv 0.77, LIF 1.0741), все максимально заплечённые:

| HF | долг, KAT | приз (LIF−1)×repaid | net боевым `evaluate()` | чанк |
|---|---|---|---|---|
| 1.001352 | 25,908,773 | $8,697 | **$733** | 25% (полный не влезает в пул) |
| 1.001490 | 1,187,154 | $398 | $194 | 100% |
| 1.008803 | 8,885 | $2.98 | $1.56 | 100% |
| 1.013772 | 482,610 | $162 | $82 | 100% |
| 1.015779 | 1,271,414 | $427 | $207 | 100% |
| 1.022488 | 441,109 | $148 | $75 | 100% |
| 1.037987 | 264,973 | $89 | $46 | 100% |

Приз кластера **$9,924 гросс**, реализуемый net **≈$1,340** — глубина пула съедает
остальное. Цена KAT проверена независимо живой котировкой: $0.004515/KAT на 100k
(impact 0.28%), $0.004425 на 1M (2.26%). Для сравнения: приз ВСЕГО чейна за 30 суток
был **$25.92**. Одна эта позиция — в 28 раз больше месячного потока.

### Дефект: hot-петля не видит кластер (нужно решение kelbic)

`analysis/monitor.py:344` — `hot_rows = sorted(..., key=lambda r: -(r.get("debt_usd") or 0))`.
Долг в KAT непрайсуем (`debt_usd()` знает только стейблы + грубые ETH/BTC), `None` → 0 →
**все неоценимые позиции падают на дно списка**. При `HOT_MAX_N=25` и 386 near-edge
строках кластер оказывается на местах #377–383 из 386 — то есть целиком вне hot-петли.
Проверено воспроизведением живого прохода: наша $733-цель — **#383**.

Практический смысл: рынок перечитывается только полным API-проходом (~30с) вместо
hot-каденса (1–5с). При 11 ликвидаторах на чейне это разница между взятием и наблюдением.
Топ-25 сейчас занят weETH/vbETH-кластером ($3.1M…$42k долга) — оценимым, но с призом,
который у нас же и не проходит порог.

**Корень один и тот же, что у пола прибыли (запись 29.07):** непрайсуемый займ. Один фикс
закрывает оба: прайсить KAT (живой котировкой Sushi с кэшем, как `refresh_eth_usd`) и AUSD
(стейбл $1). Тогда и `usd_floor_wei` перестаёт вырождаться в 1 wei, и сортировка hot-set
становится настоящей. Правка боевой логики решений ⇒ жду «го».

### Gate 1 (форк-реплей) — путь через новый коллатерал ПРОЙДЕН

anvil-форк Katana, код оракула `0xc5d4f6f7…` подменён заглушкой (цена −0.64% ⇒ HF=0.995),
дальше боевые модули без правок: `assess → size_liquidation → evaluate → liquidate_calldata
→ KatanaLiquidator.liquidate()`.

- `0xa9b079d27e` (полное закрытие): status **0x1**, газ 355,263, владельцу **+50,920.9 KAT
  (~$230)**; прогноз `evaluate` 50,930 — расхождение **0.02%**.
- `0x6655459bab` (чанк 25%): status **0x1**, газ 354,517, владельцу **+203,790 KAT (~$923)**;
  прогноз 203,838 — расхождение 0.02%.

Модель точна, маршрут avKAT→KAT (Sushi RP `0xac4c6e21…`) исполняется, approve/sweep
контракта работают на новом коллатерале.

**Побочный урок каскада (важный).** Первый прогон крупной позиции реверти́л
(`0x63ecb9f6` = min-out роутера) — потому что предыдущая ликвидация в том же форке уже
сдвинула пул, а квота пришла с живого чейна. На чистом форке та же позиция прошла. Это
не артефакт теста, а модель реального каскада: **два выстрела подряд в один тонкий пул
ревертят второй**, т.к. квота слепа к ещё не включённой sibling-транзакции.
`_partial_note` кэширует только размеры, про успешный свой же свап он не знает.
Кандидат-защита: после fire помечать пару (coll,loan) как «пул сдвинут» на TTL и
пере-квотить, а не стрелять по квоте того же прохода.

### Механика: чем этот кластер детонирует

Не таймер и не волатильность. Оракул avKAT/KAT — цена доли вольта, она **монотонно
растёт** (замер: +28%/год за 30 суток, скачками при харвесте), долг капает 1.64%/год при
utilization 90.2%. Дрейф HF замерен на исторических блоках: **+0.0027/день** — сами
позиции не пересекут. Пересечение возможно двумя путями: (1) заёмщик до-занимает и
перебирает (видно в истории: `0xe29b7303` за неделю уронил HF 1.029→1.0128 своим же
займом); (2) разрыв между харвестами вольта, когда проценты идут, а цена стоит — замерено
−0.000036/день на плоской цене, до 1.0 от 1.0014 это ~37 суток.

⇒ Триггер — событие `Borrow` самого заёмщика, в том же блоке. Ловится не оракульным
предсказанием, а hot-каденсом по этому рынку. Ещё одна причина закрыть дефект сортировки.

**Ничего боевого не менялось:** ни одной live-транзакции, конфиг/env не тронуты, бот не
перезапускался, anvil погашен.

### 2026-07-30, вечер — три «го» kelbic исполнены + вотчер Borrow

**1. Прайсинг KAT/AUSD (корень обеих дыр).** `AUSD` добавлен в `TOKENS`+`STABLES` (проверено
он-чейн: decimals 6, курс $0.999956 на 1k / $0.999474 на 50k). `KAT` получил живую цену:
`refresh_kat_usd()` — котировка Sushi KAT→vbUSDC **пробой фиксированного размера 100k**
(impact 0.28%), TTL 5 мин, коридор 0.0001–1.0, значение уходит в `monitor.set_token_usd()`.

*Доктринальная граница kelbic записана прямо в коде* (`monitor.py`, `_loan_usd_px`): пул-цена
— только для ранжирования, префильтра и перевода порога $20 в wei займа. Прибыль решает
`evaluate()` по реальной квоте выхода. Проба намеренно НЕ нашего сайза и снимается только на
API-проходе (не в hot-петле и не в fire-пути), поэтому цена всегда взята ДО нашего выстрела —
петли «сам сдвинул пул → сам подтвердил прибыльность» не возникает.

**2. Пол $20 включился сам** — `MIN_PROFIT_USD=20` был дефолтом, но при `loan_px=None`
вырождался в 1 wei. Теперь на AUSD/KAT-рынках порог настоящий. Побочно: пылевые цели на
AUSD (долг $0.0025) больше не проходят гейт `MIN_DEBT=$500` и отсеиваются ещё в мониторе —
выстрела уровня 29.07 ($0.04 чистыми) больше не будет.

**3. Очередь по призу, а не по долгу.** Тот же `-(debt_usd or 0)` сидел в ТРЁХ местах;
исправлены два, где кэп режет хвост: цикл выстрелов (`once`) и `_arm_candidates`. Ранг —
`_prize_usd()` = (LIF−1)×repaid в USD. Долг ≠ приз: при lltv 0.915 приз 2.6% долга, при
0.77 — 7.4%. Сортировку hot-set в мониторе НЕ трогал: прайсинга хватило (проверка ниже).

**4. Каскад-защита.** `_pool_note()/_pool_busy()` — после выстрела пара (коллатерал, займ)
помечается занятой на `KT_POOL_BUSY_TTL=20с`; остальные цели той же пары ждут следующего
прохода с честной переквотой. Это ровно тот реверт, что форк показал (`0x63ecb9f6`, min-out
роутера): квота слепа к нашей же невключённой транзакции, и переквота ВНУТРИ прохода не
помогает — помогает только ожидание включения. Порядок по призу гарантирует, что первым
ушёл самый крупный чанк.

**5. Вотчер `Borrow` (рекомендация kelbic).** `_borrow_watch()` на API-проходе: один getLogs
по узкому окну блоков, алерт 🧨, если до-занимает заёмщик с призом ≥ `KT_BORROW_WATCH_USD=$50`,
которого мы и так держим у края. Холодный старт только ставит метку (не выплёвывает историю),
разрыв окна >5000 блоков пропускается, падение getLogs не роняет проход.

**6. Строка HOTSET** — одна строка на API-проход с составом hot-набора и топ-3 по призу.
Без неё «бот поднялся» не отличить от «кластер снова за кэпом».

**Верификация (сухой прогон, блок 38675046):**
`HOTSET 25: weETH/vbETH×7 0x6691cdca×7 avKAT/KAT×5 0xcdaf57d9×4 stcUSD/vbUSDC×2` —
кластер ВНУТРИ петли (было 0 из 7, стало 5 из 7). Две невошедшие — с призами $46 и $1.56,
то есть у порога или ниже; это корректное поведение, а не остаток дефекта.

Тесты: **322 зелёных** (+19 новых: прайсинг и коридор котировки, ранг «приз, а не долг»,
непрайсуемый не выпадает из очереди, TTL пула, шесть кейсов вотчера, HOTSET). Живой Telegram
недостижим по построению — транспорт подменён, как требует конвенция флота.

### Калибровка LIF закрыта: формула подтверждена чейном

Вопрос kelbic про кривую бонуса. **LIF = 1/(0.7 + 0.3·LLTV)** — это ровно морфовская
`min(1.15, WAD/(WAD − 0.3·(WAD − lltv)))`, cursor 0.3: `1 − 0.3(1−lltv) = 0.7 + 0.3·lltv`.
Проверено не байткодом, а **исполнением**: 29 реальных ликвидаций чейна, реализованный
LIF = seized×price/repaid по цене оракула НА БЛОКЕ СОБЫТИЯ.

| lltv | формула | реализовано | событий |
|---|---|---|---|
| 0.625 | 1.126761 | 1.126655–1.126761 | 6 |
| 0.770 | 1.074114 | 1.069832–1.074094 | 4 |
| 0.860 | 1.043841 | 1.043531–1.043688 | 2 |
| 0.915 | 1.026167 | 1.026167 (в ноль) | 15 |

27 из 29 совпали (21 — точно); два «расхождения» — целочисленное округление на пыли
(repaid = 3 и 11 единиц дают ровно 2/3 и 10/11). **Кэп 1.15 в байткоде есть, но на Katana
не задействован**: минимальный lltv чейна 0.625 → 1.1268. Кэп начинает резать ниже lltv 0.5.

### 2026-07-30, поздний вечер — вотчер ставки: когда опцион получает дату

Флотовый обсчёт показал структуру, которой раньше не было в явном виде: **ни один рынок Katana
не является таймером**. Оракул везде растёт быстрее, чем капает заём, поэтому позиции у края
самозалечиваются, и время работает против нас:

| рынок | ставка займа | дрейф коллатерала (7д) | запас |
|---|---|---|---|
| weETH/vbETH | 1.23% | +2.4%/год | **+1.1%** |
| stcUSD/vbUSDC | 3.80% | +5.1%/год | +1.3% |
| avKAT/KAT | 1.64% | +7.3%/год | +5.6% |
| LBTC/vbUSDC | 2.00% | +8.3%/год | +6.3% |

87% денег чейна (7 позиций weETH/vbETH, net $37.7k) идут с перевесом **всего +1.1% годовых**.
Скачок утилизации на vbETH переводит их из «нужна дислокация» в «падают по расписанию» — это
смена РЕЖИМА, а не движение цены, и увидеть её можно только сравнив две величины, которые
проход и так читает.

`_rate_watch()` на API-проходе: для рынков с призом ≥ `KT_RATE_WATCH_USD=$50` сравнивает APR
займа с годовым дрейфом оракула по скользящему окну ≥6ч и даёт **один алерт ⏱ на смену знака**
(и один на возврат). Ставки и цены теперь отдаёт сам `scan()` — второй раз в сеть не ходим.

Тест поймал дефект в первой версии: защита «первая оценка — молчим» глушила и первый же
сигнал о перевёрнутом режиме, то есть ровно тот случай, ради которого вотчер написан. Теперь
молчит только первая оценка в НОРМАЛЬНОМ режиме.

Тесты: **329 зелёных** (+7: засечка, окно, алерт на смену знака и его однократность, возврат,
тишина при ведущем дрейфе, порог приза, устойчивость к пустым данным).

## 2026-07-30 — контракт v2: выход из yv-вольтов (написан и протестирован, НЕ задеплоен)

**Зачем.** Две позиции у края на рынках `yvvbUSDC/vbUSDT` и `yvvbUSDT/vbUSDC` дают приз
**$42,670**, и они были мертвы не по экономике, а по механике: у долей вольта **нет пула на
Sushi**, маршрута не существует в принципе. Оба вольта проверены он-чейн — честные ERC-4626:
`yvvbUSDC` над vbUSDC (totalAssets $10.1M, курс 1.0241), `yvvbUSDT` над vbUSDT ($2.6M, 1.0225).
Выход — `redeem()`, а не своп.

**Что сделано в `contracts/src/KatanaLiquidator.sol`:**
- `IERC4626` + структура `VaultExit{vault, asset}` отдельным параметром `liquidate()`;
- в колбэке: при `vault != 0` сначала `redeem(seized) -> базовый токен`, потом обычный своп.
  `asset()` **сверяется он-чейн** (`VaultAssetMismatch`) — подставить чужой `vaultAsset` в
  calldata нельзя; нулевой возврат redeem реверти́т (`RedeemFailed`);
- **своп пропускается целиком**, если база вольта совпала с займом: ни маршрута, ни
  проскальзывания, ни газа на роутер;
- подметание вынесено в `_sweepLeftovers()` и учитывает базовый токен вольта — иначе остаток
  redeem копился бы в контракте как нехеджированный инвентарь;
- `vault=0` — прежнее поведение бит-в-бит.

Девятый параметр упёрся в «stack too deep»: два адреса схлопнуты в `VaultExit`, а затем
включён `via_ir` (штатное лекарство; контракт всё равно деплоится заново).

Тесты: **12 зелёных** (7 прежних + 5 новых: redeem→своп по курсу вольта, несовпадение
`asset()`, нулевой redeem, отсутствие свопа когда база=заём, сохранение проверки `CannotRepay`).

### Что ОСТАЛОСЬ (сознательная граница — боевой бот остаётся на v1)

Деплой без интеграции бессмыслен, а переключение `KT_CONTRACT` на неинтегрированный v2
**сломало бы работающего бота**. Поэтому остановился здесь. Осталось:
1. `evaluate()` для vault-коллатерала должен котировать `previewRedeem(seized) → базовый
   токен → заём`, а не `доли → заём` (сейчас он спрашивает несуществующий маршрут);
2. `liquidate_calldata()` — новый селектор и поле `VaultExit`;
3. деплой v2 + `KT_CONTRACT` на новый адрес + форк-реплей на живой yv-позиции (как Gate 1);
4. старый контракт средств не держит (всё выметается владельцу) — миграция тривиальна.

### Gate 1 для v2 — ОБА пути пройдены на форке (деплой в бой НЕ делался)

Форк Katana, контракт v2 задеплоен на нём (940,490 газа, владелец верный), оракул подменён
заглушкой до HF 0.995, дальше боевые модули без правок при `KT_CONTRACT_V2=1`.

| путь | status | газ | владельцу | прогноз evaluate | расхождение |
|---|---|---|---|---|---|
| обычный (avKAT/KAT) | **0x1** | 342,392 | +$900.15 | $900.51 | 0.04% |
| yv-вольт (redeem) | **0x1** | **1,692,244** | **+$43,313.87** | $43,314.26 | 0.001% |

**Главный риск снят:** `via_ir` не сломал работающий путь — обычная ликвидация проходит с той
же экономикой, что и на v1 сегодня утром. **Второй риск снят:** `redeem` внутри колбэка Morpho
ведёт себя ровно как статический `previewRedeem` (расхождение 0.001%), то есть вывод из Yearn
идёт без сюрпризов со стратегиями.

**НАЙДЕНО ФОРКОМ — газ.** Путь через вольт съел **1,692,244** при `KT_GAS_LIMIT=1,800,000`:
запас всего **6%**. Иная раскладка стратегий вольта — и выстрел упрётся в лимит, сожжёт газ и
не сделает ничего. Перед переключением на v2 лимит надо поднять (предлагаю 2,600,000; по
деньгам это копейки — 1.7M газа на Katana ≈ $0.03, а upfront-чек ноды при текущем base fee
требует ~0.00016 ETH при балансе 0.000937). Это правка боевого env — жду слова.

Цифра $43.3k в таблице — это «если позиция пересечёт», на форке HF принудительно опущен до
0.995 (полное закрытие). Сегодняшняя реализуемая величина при живом HF 1.03 — $23,026.

**Статус:** код бота и контракт готовы и проверены; `KT_CONTRACT` по-прежнему указывает на v1,
`KT_CONTRACT_V2` выключен. Осталось ровно то, что вы решаете: поднять газ-лимит, задеплоить v2
в бой, переключить адрес и флаг.

### 2026-07-30 вечер — теневые замеры ушли из TG (побочка моей же правки)

Правка ранжирования пре-арма (приз вместо долга, `5cdaea2`) завела в набор кластер avKAT/KAT —
и теневой same-block слой начал мерять по каждому пушу его оракула. Замер: **10 теневых
выстрелов за 3 часа, все avKAT/KAT**, при вотчере с каденцией 15 минут это до четырёх
TG-сообщений в час о том, что НИЧЕГО не произошло (shadow вживую не стреляет).

Сам замер полезный и его стоит читать: `feasible 4/4`, пуш садится через +1 блок — мы бы
успели. Но это рутина, а по тихому режиму рутине место в логе.

`ops/shadow_watch.py` (боевая копия в `~/.katana-probe/`): в Telegram уходят только **живые**
выстрелы и **суточная сводка**; теневые замеры пишутся строкой в лог. `SHADOW_TG=1` возвращает
прежнее поведение. Проверено прогоном: `[shadow] +2 теневых замеров (avKAT/KAT) — в TG не шлём`.
Скрипт заведён в репозиторий (жил только на диске, вне гита).

### 2026-07-30 — BORROW ушёл в лог: алерт нужен там, где нужен человек

Вотчер сработал в бою впервые и точно: заёмщик крупнейшей позиции кластера
(`0x6655459bab`, приз $8,474) сделал шаг наращивания плеча и сдвинул свой HF
**1.001342 → 1.000599** — 0.06% до линии. Разбор трёх его шагов по блокам:

| шаг | HF до | HF после | сдвиг |
|---|---|---|---|
| −50.7ч | 1.001022 | 1.001120 | +0.000098 |
| −23.8ч | 1.001790 | 1.000741 | −0.001049 |
| −0.3ч | 1.001342 | **1.000599** | −0.000743 |

Механика: доходность вольта поднимает HF примерно на +0.0007/сутки, заёмщик этот запас
выбирает обратно новым займом. Он держит коридор ~1.0006–1.0018, а не сползает вслепую;
пересечение случится на ПРОМАХЕ (его же максимальный шаг −0.001049 от текущего уровня даёт
0.99955), а промах — вероятность, не дата.

**Решение kelbic:** «если бот готов к этому событию, то алерт не нужен — когда произойдёт,
тогда произойдёт». Критерий принят как общее правило: **алерт нужен там, где нужен ЧЕЛОВЕК**;
сигналить по ИСХОДУ, а не по предвестнику. Событие бот отрабатывает сам — позиция в
hot-наборе, опрос 0.3с, лестница чанков подберёт размер под пул.

`_borrow_watch` теперь пишет строкой в лог, а не в Telegram. Запись сохранена ради форензики:
когда пересечение случится, история шагов плеча — это то, по чему разбирают, кто успел.
Тест переписан так, что **ловит регресс**: если сигнал вернут в `alert()`, сьют упадёт.

Правило записано в память (`alerts-only-where-human-acts`). По нему же стоит перебрать
остальные каналы: `⏱` смена режима ставки — пограничный случай (редкий по построению и меняет
стратегию, оставил), `🏁 RACE`, выстрелы и P&L, kill-switch — исход или решение, остаются.

## 2026-07-30 ~20:00 UTC: контракт v2 (yv-вольт redeem-путь) В БОЮ

По «го» kelbic — деплой отложенного шага 3. Порядок и результаты:

**1. Тесты перед деплоем:** forge build чист, 6/6 `KatanaLiquidatorForkTest` (реальный Morpho
на форке) + 5/5 `VaultExitTest` (redeem→swap, mismatch/zero-redeem/no-swap/cannot-repay).
Форк-реплей был пройден ранее: normal-путь 342,392 газа, vault-путь 1,692,244, оба status 0x1.

**2. Деплой:** `0xd8ADeE48d94AcA1D52B60C8f6D8D516c667198C3`
(tx `0x02c9d2d8cabc928fd7d4f4547158b414a0e90ea84f0de3c003687cb6ec1b9c1a`, 940,490 газа, ~$0.01).
Верифицировано он-чейн: код на месте, owner = наш `0x3E8E…C4D5`, MORPHO верный.

**3. Переключение:** в `~/.katana-bot/env` — `KT_CONTRACT`=v2, `KT_CONTRACT_V2=1`,
`KT_GAS_LIMIT=2600000` (при дефолте 1.8M запас над vault-путём был всего 6%). Бэкап env в
jobs/tmp. Рестарт точным kill по pid (не pkill -f!), cron поднял с новым конфигом.

**4. Канарейка (сухая, живым кодом бота, изолированный KT_STATE, TG выключен):**
- hot-петля: guard=OK, DRY_RUN=OFF, 0 трейсбеков; HOTSET прежний (vault-рынки 0x6691cdca×7 +
  0xcdaf57d9×4 уже были внутри после починки прайсинга);
- `evaluate()` по двум топовым yv-позициям: `0x6655459b…` приз $23,248 → **net $21,350**
  (f=1.0, impact 0.06%), `0xba3b9a3e…` приз $20,192 → **net $18,563**. Оба через
  previewRedeem → Sushi-квота базы вольта; calldata несёт v2-селектор `0x138eb97b` и
  VaultExit-адрес. Это те ~$43k, что были заперты за v1.

**Откат:** `KT_CONTRACT=0x32156dFFB62B03028a3311736DB96bB5c792ae71` (v1 жив) +
`KT_CONTRACT_V2=0`, `KT_GAS_LIMIT` убрать, рестарт. Заметка: `once`-канарейка напомнила, что
скан-строка не несёт `borrow_shares_repaid` — его заполняет огневой путь (executor.py:2314);
воспроизводить при любых ad-hoc вызовах `evaluate()`.

## 2026-07-30 ~20:25 UTC: ⏱ rate-watch — в лог; правило обобщено («боты-опционы»)

Первое же 6-часовое окно вотчера дало залп из 5 ⏱-алертов — все ЛОЖНЫЕ. Две причины,
обе починены:
1. **Окно 6ч статистически пусто** для этой метрики: шум котируемых оракулов аннуализируется
   в сотни %/год (в залпе было «−1657%/год»), а дрейф медленного аккруера за 6ч неотличим
   от нуля (avKAT «+0.00%» при честных +7.3%/год на 7-дневном замере). Окно → **7 дней**
   (`KT_RATE_WATCH_WINDOW_SEC=604800`), тот же горизонт, что у ручного замера дрейфа.
2. **Смена режима — предвестник, не исход.** Человек по ней ничего не делает: цели и так в
   hot-наборе. Сигнал переведён В ЛОГ, тест переписан регресс-ловушкой (вернут alert() —
   сьют упадёт). 335 зелёных.

**Формулировка kelbic, принятая как общая:** «алерты в TG в принципе не нужны, если не
требуется действие человека — это боты-опционы». Ревизия всех alert() executor'а по ней:
🔫/⚡ выстрелы, ✅/❌ исход+P&L, 📬 расчёт зависшего, 🏁 проигранная гонка — исходы, остаются;
⚠️ stuck nonce / error огневого пути, KILL-SWITCH — нужен человек, остаются; 🔬 «tip
победителя выше нашего дефолта» — триггер Фазы 2, решение владельца, остаётся; 💓 heartbeat
выключен конфигом. Предвестников в TG больше нет: 🧨 BORROW и ⏱ — в логе.

## 2026-08-01 04:32 UTC — ИНЦИДЕНТ: 11 часов слепоты при живом процессе

**Что произошло.** 31.07 в **17:35:22** сканер блоков написал последнюю строку и встал.
Тревога 💀 пришла только в **04:32** — через 11 часов. Отставание на момент подъёма:
последний просканированный блок 38776510 против головы 38816016 = **39,506 блоков**.

**Почему не сработал ни один прибор.**
- `cron`-watchdog проверяет, что процесс ЖИВ — процесс был жив (pid 2091880, аптайм 32ч).
- `flock` держался тем же живым процессом, поэтому respawn не происходил.
- Деадман мерил `stat -c %Y executor.log`. В этот лог пишет **не один поток**: сканер
  блоков и мемпул-слой. Мемпул продолжал писать `MEMPOOL …` ещё ~9.5 часов после смерти
  сканера, файл оставался «свежим», и деадман молчал. Он проснулся только тогда, когда
  замолчал и мемпул.

**Состояние процесса в момент разбора (снято до рестарта):** state `R`, **293 тика CPU за
3 секунды ≈ 98% ядра**, 4 потока (1 R + 3 S), RSS упал 59МБ → 31МБ, 8 сокетов. То есть не
дедлок, а **спин в участке без логирования**. Последние строки перед тишиной — цикл
переподключения WSS (`WSS dropped … reconnecting in 0.5s`), 5 реконнектов в последних 40
строках. Гипотеза: шторм реконнектов ушёл в тесную петлю. Не доказано — py-spy на машине
нет, `debug`-инструментов тоже; чтобы закрыть, нужен стек живого процесса.

**Действия.** Рестарт по точному pid → 3006683, блок 38816096, голову догнал, `guard=OK`.

**Починка прибора (важнее самого рестарта).** Деадман переведён с mtime файла на **возраст
последней строки ГЛАВНОГО ЦИКЛА** (`[HH:MM:SS] block …`). Проверено на самом инциденте:
новая версия посчитала возраст 39,574с и корректно обработала отсутствие даты в отметке
(переход через полночь). В текст алерта добавлен признак «процесс жив» — именно он отличает
этот класс отказа от обычной смерти. Тот же дефект найден и закрыт у **всех трёх остальных**
ботов (у них посторонних писателей в логе сейчас нет, то есть дефект был латентным).
Скрипты сведены в `kelbic/fleet-watch` (`deadmen/`) и подставлены симлинками по прежним
cron-путям: три из четырёх жили только в рантайме. Коммит a6d1efc.

**Что осталось не выясненным:** ПРИЧИНА спина. Пока стоит наблюдение: если повторится,
снимать стек живого процесса (поставить py-spy заранее) — рестарт без причины лечит
симптом. Отдельно проверить, не связан ли спин с нагрузкой хоста (в 20:37 31.07
load average был 3.46 при 4 ботах).
