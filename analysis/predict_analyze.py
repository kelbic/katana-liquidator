#!/usr/bin/env python3
"""Сводка по PREDICT-логу katana (оракул-предсказание, shadow).

Отвечает: реальный lead-time (Binance пересёк порог → on-chain пуш), FP-rate и recall
по НАШЕМУ живому потоку — валидация ресёрча (~30-40с медиана, ~46% FP, ~72% recall).

RECALL СЧИТАЕТСЯ ЧЕСТНО (--chain, по умолчанию вкл.). Наивная формула
`confirmed/(confirmed+push)` берёт знаменателем только те пуши, которые МЫ ЗАМЕТИЛИ:
пуш, случившийся при лежащем боте или оборванном Binance-потоке, не попадёт ни в
confirmed, ни в push — знаменатель занижен, recall завышен. Поэтому знаменатель берём
из блокчейна: все AnswerUpdated агрегаторов BTC/USD и ETH/USD за то же окно.
(Проверка 19.07: on-chain 28 против 25+3 в логе — совпало, recall 89% НЕ был завышен.
Это не повод доверять наивной формуле дальше: следующее окно может содержать простой,
и тогда разойдётся. Плюс печатаем покрытие — сколько времени бот реально смотрел.)

Usage:
    python3 ~/.katana-probe/predict_analyze.py          # с он-чейн сверкой
    python3 ~/.katana-probe/predict_analyze.py --no-chain   # только по логу (офлайн)
"""
import json
import os
import sys
import urllib.request

LOG = os.path.expanduser("~/.katana-bot/executor.log")
RPC = "https://rpc.katanarpc.com"
HDRS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
# topic0 = keccak("AnswerUpdated(int256,uint256,uint256)") — считаем в коде, не хардкодим хэш
_ANSWER_UPDATED_SIG = "AnswerUpdated(int256,uint256,uint256)"
# агрегаторы ровно тех фидов, которые предсказывает слой (bot/predict.SYMBOL_FEED)
AGGREGATORS = {
    "0x56ac2b1b78225d47993e8866795a34ad540a515c": "BTC",
    "0x47522e7273344f1016a1e67e496ddb4f77d852c9": "ETH",
}
# перерыв в PREDICT-событиях длиннее этого = бот не смотрел (рестарт/обрыв фида)
GAP_SEC = 900.0


def topic0() -> str:
    from eth_utils import keccak
    return "0x" + keccak(text=_ANSWER_UPDATED_SIG).hex()


def parse(line):
    d = {}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            d[k] = v
    return d


def rpc(method, params, tries=3):
    import time
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                RPC, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                 "params": params}).encode(), HDRS)
            d = json.load(urllib.request.urlopen(req, timeout=30))
            if "result" in d:
                return d["result"]
            last = d.get("error")
        except Exception as e:      # noqa: BLE001 — диагностика, не боевой путь
            last = e
        if i < tries - 1:
            time.sleep(0.6)
    raise RuntimeError(f"{method}: {last}")


def block_at(ts: float, head: int, head_ts: int) -> int:
    """Номер блока по времени. Katana ~1с/блок; уточняем двумя пробами, потому что
    ошибка в границе окна утащила бы в знаменатель чужие пуши."""
    b = max(1, head - int(head_ts - ts))
    for _ in range(2):
        got = int(rpc("eth_getBlockByNumber", [hex(b), False])["timestamp"], 16)
        drift = int(got - ts)
        if abs(drift) <= 2:
            break
        b = max(1, b - drift)
    return b


def onchain_pushes(t0: float, t1: float) -> dict:
    """Все обновления отслеживаемых агрегаторов в окне [t0, t1] — честный знаменатель."""
    head = int(rpc("eth_blockNumber", []), 16)
    head_ts = int(rpc("eth_getBlockByNumber", [hex(head), False])["timestamp"], 16)
    b0, b1 = block_at(t0, head, head_ts), block_at(t1, head, head_ts)
    tp, out = topic0(), {}
    for addr, name in AGGREGATORS.items():
        logs, b = [], b0
        while b <= b1:
            e = min(b + 9999, b1)
            logs += rpc("eth_getLogs", [{"address": addr, "fromBlock": hex(b),
                                         "toBlock": hex(e), "topics": [tp]}]) or []
            b = e + 1
        out[name] = len(logs)
    return out


def coverage(rows) -> tuple[float, float, list]:
    """Доля окна, когда бот РЕАЛЬНО смотрел. Дыры (рестарт, обрыв Binance-фида) означают
    пуши, которых нет ни в одном событии лога — именно из-за них наивный recall врёт.

    Считается по СТРОКАМ СТАТУСА (тикают ~1/с), а НЕ по PREDICT-событиям: последние
    редки по своей природе (взвод/пуш случаются несколько раз в час), и первая версия
    этой функции показала «покрытие 19%, 18 дыр» на полностью живом боте. Ложная
    тревога в мониторинге хуже её отсутствия — метрика обязана мерить простой, а не
    затишье."""
    span = ts_first = ts_last = None
    ticks = []
    import re
    from datetime import datetime, timezone
    day = None
    for line in open(LOG, errors="ignore"):
        m = re.match(r"\[(\d{2}):(\d{2}):(\d{2})\] block ", line)
        if not m:
            continue
        h, mi, s = (int(x) for x in m.groups())
        sec = h * 3600 + mi * 60 + s
        if ticks and sec < ticks[-1] % 86400:     # переход через полночь
            day = (day or 0) + 1
        ticks.append((day or 0) * 86400 + sec)
    if len(ticks) < 2:
        return 0.0, 0.0, []
    span = ticks[-1] - ticks[0]
    gaps = [(a, b) for a, b in zip(ticks, ticks[1:]) if b - a > GAP_SEC]
    lost = sum(b - a for a, b in gaps)
    return span, (span - lost) / span if span else 0.0, gaps


def main():
    use_chain = "--no-chain" not in sys.argv
    rows = []
    for line in open(LOG, errors="ignore"):
        i = line.find("PREDICT ")
        if i >= 0:
            rows.append(parse(line[i + 8:]))

    by = lambda ev: [r for r in rows if r.get("event") == ev]   # noqa: E731
    arm, conf, push = by("arm"), by("confirmed"), by("push")
    disarm, fp = by("disarm"), by("falsepos")

    print(f"события: arm={len(arm)} confirmed={len(conf)} push_no_arm={len(push)} "
          f"disarm={len(disarm)} falsepos={len(fp)}")

    span, cov, gaps = coverage(rows)
    if span:
        print(f"окно {span / 3600:.1f}ч | покрытие {cov:.0%}"
              + (f" | дыр >{GAP_SEC / 60:.0f}мин: {len(gaps)}" if gaps else ""))

    if conf:
        leads = sorted(float(r["lead_s"]) for r in conf if r.get("lead_s") not in (None, "-"))
        if leads:
            n = len(leads)
            print(f"\nLEAD-TIME (Binance-порог → on-chain пуш), n={n}:")
            print(f"  медиана {leads[n // 2]:.0f}s | p25 {leads[n // 4]:.0f}s | "
                  f"p90 {leads[int(n * 0.9)]:.0f}s | min {leads[0]:.0f}s")
            print(f"  (ресёрч: медиана ~30-40s, min ~13-26s; наша mempool-фора ~0.6s)")
            tight = [x for x in leads if x < 15]
            if tight:
                print(f"  ⚠ {len(tight)}/{n} с lead<15s — окна, где подготовиться почти нереально")

    if arm:
        fprate = (len(disarm) + len(fp)) / len(arm)
        print(f"\nFP-rate = (disarm+falsepos)/arm = {len(disarm) + len(fp)}/{len(arm)} "
              f"= {fprate:.0%}  (ресёрч ~46%)")
        held = [float(r["held_s"]) for r in disarm + fp if r.get("held_s")]
        if held:
            print(f"  цена ложняка: {len(held)} взводов, всего {sum(held) / 60:.0f} мин впустую")

    seen = len(conf) + len(push)
    if seen:
        print(f"\nrecall (наивный, знаменатель = ЧТО МЫ ЗАМЕТИЛИ) = {len(conf)}/{seen} "
              f"= {len(conf) / seen:.0%}")
    if use_chain and rows:
        ts = [float(r["ts"]) for r in rows if r.get("ts")]
        try:
            oc = onchain_pushes(min(ts), max(ts))
            total = sum(oc.values())
            if total:
                print(f"recall (ЧЕСТНЫЙ, знаменатель = все пуши on-chain) = {len(conf)}/{total} "
                      f"= {len(conf) / total:.0%}  (ресёрч ~72%)")
                print(f"  on-chain пушей: {oc} | пропущено мимо лога: {total - seen}")
                if total != seen:
                    print("  ⚠ лог НЕ видел часть пушей — наивная цифра завышена")
        except Exception as e:      # noqa: BLE001 — офлайн не должен ронять разбор
            print(f"recall (честный): недоступен — {str(e)[:70]}; см. --no-chain")

    by_feed = {}
    for r in conf:
        by_feed.setdefault(r.get("feed", "?"), []).append(r)
    if by_feed:
        print("\nпо фидам (confirmed):", {k: len(v) for k, v in by_feed.items()})

    if not rows:
        print("\n[PREDICT-событий ещё нет — нужен ход цены Binance >=0.45% от якоря + оракул-пуш]")
    elif not conf:
        print("\n[arm'ы есть, но confirmed ещё нет — ждём пуш после взвода]" if arm
              else "\n[пока только bootstrap — ждём движения цены к порогу]")


if __name__ == "__main__":
    main()
