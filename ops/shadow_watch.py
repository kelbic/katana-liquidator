#!/usr/bin/env python3
"""Авто-вотч same-block выстрелов katana (shadow/live). Пингует TG при появлении НОВЫХ
MEMPOOL shadow_fire/live_fire. Идемпотентен через seen-файл ts (устойчив к ротации лога).
Первый запуск = baseline (запоминает существующие, не алертит). Каденс: cron."""
import json
import os
import time
import urllib.parse
import urllib.request

LOG = os.path.expanduser("~/.katana-bot/executor.log")
SEEN = os.path.expanduser("~/.katana-probe/.shadow_watch_seen")
DIGEST = os.path.expanduser("~/.katana-probe/.shadow_watch_digest")
ENV = os.path.expanduser("~/.katana-bot/env")
TG_ENV = os.path.expanduser("~/.claude/channels/telegram/.env")


def parse(line):
    d = {}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            d[k] = v
    return d


def tg(text):
    token = chat = None
    try:
        for ln in open(TG_ENV):
            if ln.startswith("TELEGRAM_BOT_TOKEN="):
                token = ln.split("=", 1)[1].strip()
    except OSError:
        pass
    try:
        for ln in open(ENV):
            if "KT_CHAT_ID=" in ln:
                chat = ln.split("=", 1)[1].split()[0].strip()
    except OSError:
        pass
    if not token or not chat:
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    try:
        r = json.loads(urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=15).read())
        return bool(r.get("ok"))
    except Exception:
        return False


def main():
    rows = []
    try:
        for l in open(LOG, errors="ignore"):
            i = l.find("MEMPOOL ")
            if i >= 0:
                rows.append(parse(l[i + 8:]))
    except OSError:
        return
    fires = [r for r in rows if r.get("event") in ("shadow_fire", "live_fire") and r.get("ts")]
    landed = {r.get("oracle_tx"): r for r in rows if r.get("event") == "landed"}

    seen = set()
    have_seen = os.path.exists(SEEN)
    if have_seen:
        try:
            seen = set(open(SEEN).read().split())
        except OSError:
            pass

    new = [r for r in fires if r["ts"] not in seen]

    # первый запуск: baseline без алерта (не стрелять задним числом)
    if not have_seen:
        with open(SEEN, "w") as f:
            f.write("\n".join(r["ts"] for r in fires) + ("\n" if fires else ""))
        return
    if not new:
        return

    feas = sum(1 for r in new if r.get("feasible") == "1")
    live = [r for r in new if r.get("event") == "live_fire"]
    markets = sorted({r.get("market", "?") for r in new})
    # сверка с реальной посадкой пуша
    landed_bits = []
    for r in new:
        lr = landed.get(r.get("oracle_tx"))
        if lr and lr.get("blocks_after"):
            landed_bits.append(lr["blocks_after"])
    landed_str = (", ".join(f"+{b}" for b in landed_bits) if landed_bits else "нет данных")
    tag = "LIVE 🔴" if live else "SHADOW"
    msg = (f"🎯 katana same-block ({tag}): +{len(new)} выстрел(ов)\n"
           f"feasible (успели бы в блок): {feas}/{len(new)}\n"
           f"пуш сел через: {landed_str} блок(а)\n"
           f"рынки: {', '.join(markets)[:120]}\n"
           f"→ разбор: python3 ~/.katana-probe/shadow_analyze.py")
    # 30.07: правка ранжирования пре-арма (приз вместо долга) завела в набор кластер avKAT/KAT,
    # и теневой слой начал мерять по каждому пушу его оракула — 10 замеров за 3 часа, то есть
    # до четырёх TG-сообщений в час о том, что НИЧЕГО не произошло (shadow не стреляет вживую).
    # Тихий режим: замер — рутина, ему место в логе; в Telegram идут только ЖИВЫЕ выстрелы и
    # раз в сутки сводка. SHADOW_TG=1 возвращает прежнее поведение.
    force = os.environ.get("SHADOW_TG", "0") == "1"
    digest_due = False
    try:
        digest_due = (time.time() - os.path.getmtime(DIGEST)) > 86400
    except OSError:
        digest_due = True
    if live or force:
        tg(msg)
    elif digest_due:
        tg("🕛 " + msg + f"\n(суточная сводка теневых замеров; живых выстрелов нет)")
        open(DIGEST, "w").write(str(int(time.time())))
    else:
        print(f"[shadow] +{len(new)} теневых замеров ({', '.join(markets)[:60]}) — "
              f"в TG не шлём, рутина")
    with open(SEEN, "a") as f:
        for r in new:
            f.write(r["ts"] + "\n")


if __name__ == "__main__":
    main()
