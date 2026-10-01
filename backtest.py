#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ICT бот бэктести: bot.py'даги АЙНАН ўша қоидаларни ўтган маълумотда синайди.

• Маълумот: Binance очиқ маълумоти (API калит керак эмас).
• Ҳар бир ёпилган 1H шамда bot.analyze() чақирилади — худди бот каби.
• Сигнал чиқса: кириш = 1H шам ёпилиш нархи, стоп ва мўлжал — ботдагидек.
• Натижа 15M шамлар бўйича текширилади: аввал стопми ёки мўлжалми.
  Бир шамда иккаласи бўлса — зарар деб ҳисобланади (эҳтиёткор баҳо).
• Бир тангада битта савдо: савдо ёпилмагунча янги сигнал олинмайди.
"""

import os
import time
import bisect
from datetime import datetime, timezone

import requests
import bot

# ═══════════════════════ СОЗЛАМАЛАР ═══════════════════════
DAYS       = int(os.getenv("DAYS") or 90)            # неча кунлик тарих
MAX_HOLD_H = int(os.getenv("MAX_HOLD_H") or 168)     # савдо макс. неча соат очиқ туради (168 = 7 кун)
FEE_PCT    = float(os.getenv("FEE_PCT") or 0.1)      # кириш+чиқиш комиссияси жами (%)

URL    = "https://data-api.binance.vision/api/v3/klines"
IV_MS  = {"1W": 7 * 86400000, "1d": 86400000, "4h": 4 * 3600000, "60m": 3600000, "15m": 900000}
WARMUP = bot.KLINES

CUR = {}   # ҳозирги вақтдаги маълумот бўлаклари (bot.klines ўрнига)


def fake_klines(symbol, interval, limit=bot.KLINES):
    return CUR[interval]


bot.klines = fake_klines   # бот энди интернетдан эмас, тарихдан ўқийди


# ═══════════════════════ МАЪЛУМОТ ═══════════════════════
def fetch(symbol, iv, start, end):
    out, cur = [], start
    while cur < end:
        rows = None
        for attempt in range(5):
            try:
                r = requests.get(URL, params={"symbol": symbol, "interval": bot.BIN_IV[iv],
                                              "startTime": cur, "endTime": end, "limit": 1000}, timeout=20)
                r.raise_for_status()
                rows = r.json()
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(2 * (attempt + 1))
        if not rows:
            break
        out.extend(rows)
        nxt = int(rows[-1][0]) + 1
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.15)
    seen, clean = set(), []
    for k in out:
        if k[0] not in seen:
            seen.add(k[0])
            clean.append(k)
    return {
        "t":  [int(k[0]) for k in clean],
        "ct": [int(k[6]) for k in clean],
        "o":  [float(k[1]) for k in clean],
        "h":  [float(k[2]) for k in clean],
        "l":  [float(k[3]) for k in clean],
        "c":  [float(k[4]) for k in clean],
    }


def window(d, t):
    """t вақтигача ёпилган охирги WARMUP та шам."""
    j = bisect.bisect_left(d["ct"], t)
    i = max(0, j - WARMUP)
    return {k: d[k][i:j] for k in ("o", "h", "l", "c")}


def simulate(d15, t, entry, stop, target):
    """15M шамларда: аввал стопми, мўлжалми ёки вақт тугадими."""
    i = bisect.bisect_left(d15["t"], t)
    end = t + MAX_HOLD_H * 3600000
    n = len(d15["t"])
    while i < n and d15["t"][i] < end:
        if d15["l"][i] <= stop:
            return "loss", stop, d15["ct"][i] + 1
        if d15["h"][i] >= target:
            return "win", target, d15["ct"][i] + 1
        i += 1
    if i >= n:
        last = d15["c"][-1] if n else entry
        return "open", last, None
    return "timeout", d15["c"][i - 1], d15["ct"][i - 1] + 1


def ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")


# ═══════════════════════ БЭКТЕСТ ═══════════════════════
def run_symbol(sym, test_start, now):
    data = {}
    for _, iv in bot.TREND_TFS:
        data[iv] = fetch(sym, iv, test_start - WARMUP * IV_MS[iv], now)
    h1, trades, busy_until = data["60m"], [], 0
    for ct in h1["ct"]:
        if ct < test_start or ct >= now:
            continue
        t = ct + 1
        if t < busy_until:
            continue
        for _, iv in bot.TREND_TFS:
            CUR[iv] = window(data[iv], t)
        try:
            r = bot.analyze(sym)
        except Exception:
            continue
        if not r:
            continue
        entry, stop, target = r["price"], r["inval"], r["target"]
        res, exit_px, exit_t = simulate(data["15m"], t, entry, stop, target)
        risk = entry - stop
        fee = entry * FEE_PCT / 100
        trades.append({
            "sym": sym, "t": t, "res": res, "entry": entry, "stop": stop, "target": target,
            "rr": r["rr"], "R": (exit_px - entry - fee) / risk,
            "pct": (exit_px - entry) / entry * 100 - FEE_PCT,
            "zones": ", ".join(n for n, _, _ in r["hits"]),
        })
        busy_until = exit_t if exit_t else float("inf")
    return trades


def stats(tr):
    closed = [x for x in tr if x["res"] != "open"]
    n = len(closed)
    if n == 0:
        return None
    wins = sum(1 for x in closed if x["res"] == "win")
    losses = sum(1 for x in closed if x["res"] == "loss")
    tos = sum(1 for x in closed if x["res"] == "timeout")
    gp = sum(x["R"] for x in closed if x["R"] > 0)
    gl = -sum(x["R"] for x in closed if x["R"] < 0)
    eq = peak = dd = 0.0
    for x in sorted(closed, key=lambda z: z["t"]):
        eq += x["R"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    avg_rr = sum(x["rr"] for x in closed) / n
    return {
        "n": n, "wins": wins, "losses": losses, "tos": tos,
        "wr": wins / n * 100, "totR": sum(x["R"] for x in closed),
        "avgR": sum(x["R"] for x in closed) / n,
        "pf": gp / gl if gl > 0 else float("inf"),
        "dd": dd, "avg_rr": avg_rr, "be_wr": 100 / (1 + avg_rr),
        "totpct": sum(x["pct"] for x in closed),
    }


def verdict(s):
    if s is None:
        return "Сигнал бўлмади — фильтрлар бу даврда жуда қаттиқ."
    if s["n"] < 20:
        return f"Савдолар кам ({s['n']} та) — ишончли хулоса учун камида 20–30 та керак. Кўпроқ кун билан қайта синанг."
    if s["totR"] > 0 and s["pf"] >= 1.3:
        return "Натижа ижобий. Лекин бу ўтмиш — келажакни кафолатламайди. Аввал кичик сумма ёки қоғозда синанг."
    if s["totR"] > 0:
        return "Натижа бироз ижобий, лекин заиф. Комиссия ва кечикиш уни ейиши мумкин."
    return "Натижа салбий — бу қоидалар билан бу даврда пул йўқотилган бўларди."


def main():
    now = int(time.time() * 1000)
    test_start = now - DAYS * 86400000
    all_tr = []
    for sym in bot.SYMBOLS:
        print(f"⏳ {sym}: маълумот юкланмоқда ва синалмоқда...", flush=True)
        try:
            tr = run_symbol(sym, test_start, now)
        except Exception as ex:
            print(f"⚠️ {sym}: {ex}")
            continue
        print(f"   {sym}: {len(tr)} та сигнал", flush=True)
        all_tr += tr

    lines = [f"# ICT бот бэктести — охирги {DAYS} кун",
             f"Танглар: {', '.join(bot.SYMBOLS)} · комиссия {FEE_PCT}% · макс. ушлаш {MAX_HOLD_H} соат", ""]
    s = stats(all_tr)
    if s:
        lines += [
            "## Умумий натижа",
            "| Кўрсаткич | Қиймат |", "|---|---|",
            f"| Ёпилган савдолар | {s['n']} |",
            f"| ✅ Мўлжал / ❌ Стоп / ⏱ Вақт тугади | {s['wins']} / {s['losses']} / {s['tos']} |",
            f"| Ютуқ фоизи | **{s['wr']:.1f}%** (зарарсизлик чегараси ≈ {s['be_wr']:.0f}%) |",
            f"| Ўртача режа R:R | 1:{s['avg_rr']:.1f} |",
            f"| Жами натижа | **{s['totR']:+.1f}R** (ҳар савдода 1% риск = {s['totR']:+.1f}%) |",
            f"| Ўртача савдо | {s['avgR']:+.2f}R |",
            f"| Profit factor | {s['pf']:.2f} |",
            f"| Энг катта пасайиш | -{s['dd']:.1f}R |",
            "",
        ]
    lines += ["## Хулоса", verdict(s), ""]

    lines += ["## Тангалар бўйича", "| Танга | Савдо | Ютуқ % | Жами R |", "|---|---|---|---|"]
    for sym in bot.SYMBOLS:
        ss = stats([x for x in all_tr if x["sym"] == sym])
        lines.append(f"| {sym} | {ss['n']} | {ss['wr']:.0f}% | {ss['totR']:+.1f}R |" if ss else f"| {sym} | 0 | — | — |")
    lines.append("")

    icon = {"win": "✅", "loss": "❌", "timeout": "⏱", "open": "⏳"}
    lines += ["## Охирги 40 та савдо (UTC)", "| Вақт | Танга | Натижа | R | Сабаб |", "|---|---|---|---|---|"]
    for x in sorted(all_tr, key=lambda z: z["t"])[-40:]:
        lines.append(f"| {ts(x['t'])} | {x['sym']} | {icon[x['res']]} | {x['R']:+.2f} | {x['zones']} |")

    text = "\n".join(lines)
    print("\n" + text)
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
