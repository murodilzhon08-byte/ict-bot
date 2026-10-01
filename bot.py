#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ICT спот сигнал боти: MEXC (очиқ маълумот) → Telegram

• MEXC аккаунтига уланмайди, API калит керак эмас, савдо қилмайди.
• Фақат спот харид (ХАРИД) сигналларини Telegram'га юборади.
• Мантиқ TradingView'даги индикатор билан бир хил:
  тренд (1W 1D 4H 1H 15M), Қиммат/Арзон, OB, FVG, IFVG, OTE, SSL супурилиши.
"""

import os
import json
import time
import logging
import requests

# ═══════════════════════ СОЗЛАМАЛАР ═══════════════════════
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")      # @BotFather берган токен
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", "")    # сизнинг chat id (@userinfobot)
SYMBOLS = [s.strip().upper() for s in (os.getenv("SYMBOLS") or
    "BTCUSDT,ETHUSDT,AVAXUSDT,ATOMUSDT,OPUSDT").split(",") if s.strip()]
RUN_ONCE   = os.getenv("RUN_ONCE", "") == "1"   # GitHub Actions: бир марта текшириб тугайди
STATE_FILE = os.getenv("STATE_FILE", "sent.json")  # юборилган сигналлар хотираси

SCAN_EVERY = 300          # неча секундда бир марта текширади (300 = 5 дақиқа)
ENTRY_TF   = "60m"        # кириш таймфрейми (1H)
ZONE_TFS   = ["4h", "60m"]  # OB/FVG қидириладиган таймфреймлар
TREND_TFS  = [("1W", "1W"), ("1D", "1d"), ("4H", "4h"), ("1H", "60m"), ("15M", "15m")]
TF_NAME    = {"1W": "1W", "1d": "1D", "4h": "4H", "60m": "1H", "15m": "15M"}

MIN_TREND  = 3            # 5 та ТФ'дан камида нечтаси ўсишда бўлсин
MUST_UP    = ["1D", "4H"] # шу таймфреймлар албатта ўсишда бўлсин (катта ТФ йўналиши)
MIN_RR     = 2.0          # мин. риск/фойда нисбати (мўлжал ≥ 2 × риск)
MAX_RISK   = 2.0          # макс. риск: нархдан стопгача масофа (%)
MIN_PROFIT = 3.0          # мин. фойда: нархдан мўлжалгача масофа (%)
STOP_BUF   = 0.2          # стоп зона остидан қанча пастда (ATR 1H)
PD_LEN     = 50           # Қиммат/Арзон диапазони (шам)
PD_MAX     = 45           # шу фоиздан паст — АРЗОН (харид зонаси)
TREND_LEN  = 5            # тренд учун swing узунлиги
OB_SWING   = 3            # OB учун swing узунлиги
IMP_K      = 2.0          # OB импульс кучи (ATR)
MIN_FVG    = 0.5          # мин. FVG ўлчами (ATR)
OTE_LEN    = 10           # OTE swing узунлиги
OTE_MIN    = 3.0          # OTE оёғининг мин. ўлчами (ATR)
LIQ_LEN    = 5            # ликвидлик swing узунлиги
SWEEP_BARS = 6            # SSL охирги неча шам ичида супурилган бўлса ҳисобланади
RESEND_H   = 12           # бир хил сигнал неча соатдан кейин қайта юборилади
KLINES     = 500          # ҳар бир ТФ учун олинадиган шамлар сони

MEXC_URL    = "https://api.mexc.com/api/v3/klines"
BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"   # захира манба
BIN_IV      = {"1W": "1w", "1d": "1d", "4h": "4h", "60m": "1h", "15m": "15m"}
_use_binance = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ═══════════════════════ МАЪЛУМОТ ═══════════════════════
def _parse(raw):
    now = int(time.time() * 1000)
    rows = [k for k in raw if int(k[6]) <= now]     # фақат ёпилган шамлар
    return {
        "o": [float(k[1]) for k in rows],
        "h": [float(k[2]) for k in rows],
        "l": [float(k[3]) for k in rows],
        "c": [float(k[4]) for k in rows],
    }


def klines(symbol, interval, limit=KLINES):
    """Аввал MEXC'дан олади; MEXC жавоб бермаса — Binance'нинг очиқ маълумотидан."""
    global _use_binance
    if not _use_binance:
        try:
            r = requests.get(MEXC_URL, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=15)
            r.raise_for_status()
            return _parse(r.json())
        except Exception as ex:
            logging.warning(f"MEXC жавоб бермади ({ex}) — Binance очиқ маълумотига ўтилди")
            _use_binance = True
    r = requests.get(BINANCE_URL, params={"symbol": symbol, "interval": BIN_IV[interval], "limit": limit}, timeout=15)
    r.raise_for_status()
    return _parse(r.json())


def atr(d, n=14):
    """ATR (RMA), TradingView'даги ta.atr каби."""
    h, l, c = d["h"], d["l"], d["c"]
    out = [None] * len(c)
    if len(c) <= n:
        return out
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def is_ph(h, j, L):
    if j - L < 0 or j + L >= len(h):
        return False
    return h[j] > max(h[j - L:j]) and h[j] >= max(h[j + 1:j + L + 1])


def is_pl(l, j, L):
    if j - L < 0 or j + L >= len(l):
        return False
    return l[j] < min(l[j - L:j]) and l[j] <= min(l[j + 1:j + L + 1])


def mid_up(d, m):
    rng = d["h"][m] - d["l"][m]
    body = d["c"][m] - d["o"][m]
    return body > 0 and body >= 0.5 * rng


def mid_dn(d, m):
    rng = d["h"][m] - d["l"][m]
    body = d["o"][m] - d["c"][m]
    return body > 0 and body >= 0.5 * rng


# ═══════════════════════ ТАҲЛИЛ ═══════════════════════
def trend(d, L=TREND_LEN):
    """Структура бўйича тренд: 1 — ўсиш, -1 — тушиш, 0 — ноаниқ."""
    h, l, c = d["h"], d["l"], d["c"]
    t, lh, ll = 0, None, None
    for i in range(len(c)):
        j = i - L
        if is_ph(h, j, L):
            lh = h[j]
        if is_pl(l, j, L):
            ll = l[j]
        if lh is not None and c[i] > lh:
            t, lh = 1, None
        if ll is not None and c[i] < ll:
            t, ll = -1, None
    return t


def pd_pct(d):
    """Нарх охирги PD_LEN шам диапазонининг неча фоизида."""
    hi = max(d["h"][-PD_LEN:])
    lo = min(d["l"][-PD_LEN:])
    return 50.0 if hi <= lo else (d["c"][-1] - lo) / (hi - lo) * 100


def fvg_zones(d, a):
    """Тўлмаган бычий FVG'лар ва бычий IFVG'лар (бузилган медвежий FVG)."""
    h, l, c = d["h"], d["l"], d["c"]
    n, out = len(c), []
    for i in range(2, n):
        if a[i] is None:
            continue
        # Бычий FVG
        if l[i] > h[i - 2] and mid_up(d, i - 1) and l[i] - h[i - 2] > a[i] * MIN_FVG:
            top, bot, alive = l[i], h[i - 2], True
            for k in range(i + 1, n):
                if c[k] < bot or l[k] <= bot:   # бузилди ёки тўлди
                    alive = False
                    break
            if alive:
                out.append(("FVG", top, bot))
        # Медвежий FVG → тепасидан ёпилса бычий IFVG
        if h[i] < l[i - 2] and mid_dn(d, i - 1) and l[i - 2] - h[i] > a[i] * MIN_FVG:
            top, bot, conv = l[i - 2], h[i], None
            for k in range(i + 1, n):
                if c[k] > top:
                    conv = k
                    break
                if h[k] >= top:               # фақат тўлди — IFVG бўлмади
                    break
            if conv is not None:
                alive = all(c[k] >= bot for k in range(conv + 1, n))
                if alive:
                    out.append(("IFVG", top, bot))
    return out


def ob_zones(d, a):
    """Ҳақиқий бычий OB'лар: BOS (тана), displacement (FVG), импульс, 3 шамда чиқиш, 50% бузилмаган."""
    o, h, l, c = d["o"], d["h"], d["l"], d["c"]
    n, L = len(c), OB_SWING
    lh, hi, out = None, None, []
    for i in range(n):
        j = i - L
        if is_ph(h, j, L):
            lh, hi = h[j], j
        if lh is not None and a[i] is not None and c[i] > lh:
            rng = max(1, min(i - hi, 50))
            idx = min(range(i - rng, i), key=lambda x: l[x])
            jj = idx
            for k in range(i - 1, max(idx - 4, -1), -1):
                if c[k] < o[k] and l[k] <= l[idx] + 0.5 * a[i]:
                    jj = k
                    break
            top, bot = h[jj], l[jj]
            strong = max(h[jj:i + 1]) - bot >= IMP_K * a[i]
            eng = any(c[k] > top for k in range(jj + 1, min(jj + 4, i + 1)))
            disp = any(l[m] > h[m - 2] and mid_up(d, m - 1) for m in range(jj + 2, min(i + 4, n)))
            if strong and eng and disp:
                mid = (top + bot) / 2
                if all(c[k] >= mid for k in range(i + 1, n)):
                    out = [z for z in out if not (z[1] > bot and z[2] < top)]   # устма-уст эскисини олиб ташлаш
                    out.append(("OB", top, bot))
            lh = None
    return out


def ote_zone(d, a):
    """Охирги оёқ кўтарилиш бўлса — OTE (0.62–0.79) харид зонаси."""
    h, l, c = d["h"], d["l"], d["c"]
    n, L = len(c), OTE_LEN
    lph = lpl = None
    for j in range(L, n - L):
        if is_ph(h, j, L):
            lph = j
        if is_pl(l, j, L):
            lpl = j
    if lph is None or lpl is None or lph < lpl or a[-1] is None:
        return None
    hx, lo = max(h[lpl:]), l[lpl]
    lg = hx - lo
    if lg < OTE_MIN * a[-1] or c[-1] <= lo:
        return None
    return ("OTE", hx - 0.62 * lg, hx - 0.79 * lg)


def ssl_swept(d):
    """Охирги SWEEP_BARS шам ичида swing low найза билан супурилиб, тепада ёпилганми."""
    l, c = d["l"], d["c"]
    n, L = len(c), LIQ_LEN
    for k in range(max(0, n - SWEEP_BARS), n):
        for p in range(L, k - L):
            if is_pl(l, p, L) and l[k] < l[p] < c[k] and min(l[p + 1:k]) >= l[p]:
                return True
    return False


def bsl_levels(d):
    """Нархдан юқоридаги барча олинмаган swing high'лар (BSL), яқинидан узоғига."""
    h, c = d["h"], d["c"]
    n, L, out = len(c), LIQ_LEN, []
    for p in range(L, n - L):
        if is_ph(h, p, L) and h[p] > c[-1] and max(h[p + 1:]) < h[p]:
            out.append(h[p])
    return sorted(out)


# ═══════════════════════ СИГНАЛ ═══════════════════════
def fp(x):
    """Нархни чиройли форматлаш."""
    if x is None:
        return "—"
    d = 2 if x >= 100 else 4 if x >= 1 else 6 if x >= 0.01 else 8
    return f"{x:.{d}f}"


def analyze(sym):
    data = {iv: klines(sym, iv) for _, iv in TREND_TFS}
    tr = [(nm, trend(data[iv])) for nm, iv in TREND_TFS]
    n_up = sum(1 for _, t in tr if t == 1)

    e = data[ENTRY_TF]
    if len(e["c"]) < PD_LEN + 20:
        return None
    pct = pd_pct(e)
    tmap = dict(tr)
    if n_up < MIN_TREND or pct > PD_MAX:
        return None
    if any(tmap.get(nm) != 1 for nm in MUST_UP):    # 1D ва 4H ўсишда бўлиши шарт
        return None

    last_l, last_c = e["l"][-1], e["c"][-1]
    hits = []
    for iv in ZONE_TFS:
        d = data[iv]
        a = atr(d)
        for kind, top, bot in ob_zones(d, a) + fvg_zones(d, a):
            if last_l <= top and last_c >= bot:      # охирги шам зонага кирган ва остида ёпилмаган
                hits.append((f"{kind} {TF_NAME[iv]}", top, bot))
    ote = ote_zone(e, atr(e))
    if ote and last_l <= ote[1] and last_c >= ote[2]:
        hits.append(("OTE 1H", ote[1], ote[2]))
    if not hits:
        return None

    # Стоп: нархга энг яқин зонанинг остидан бироз пастда
    ea = atr(e)[-1] or 0.0
    inval = max(b for _, _, b in hits) - STOP_BUF * ea
    risk = last_c - inval
    if risk <= 0:
        return None
    risk_pct = risk / last_c * 100
    if risk_pct > MAX_RISK:                          # риск 2% дан ошса — сигнал йўқ
        return None

    # Мўлжал: энг яқин BSL етарли бўлмаса — кейингисини текширади (1H ва 4H)
    levels = sorted(set(bsl_levels(e) + bsl_levels(data["4h"])))
    target = next((t for t in levels
                   if t - last_c >= MIN_RR * risk                       # риск/фойда ≥ 1:2
                   and (t - last_c) / last_c * 100 >= MIN_PROFIT), None)  # фойда ≥ 3%
    if target is None:
        return None

    return {
        "sym": sym,
        "price": last_c,
        "trend": tr,
        "n_up": n_up,
        "pct": pct,
        "hits": hits,
        "swept": ssl_swept(e),
        "target": target,
        "inval": inval,
        "rr": (target - last_c) / risk,
        "risk_pct": risk_pct,
        "profit_pct": (target - last_c) / last_c * 100,
    }


def message(r):
    arrows = {1: "▲", -1: "▼", 0: "–"}
    tline = " ".join(f"{nm}{arrows[t]}" for nm, t in r["trend"])
    zones = "\n".join(f"  • {n}: {fp(b)} – {fp(t)}" for n, t, b in r["hits"])
    power = len(r["hits"]) + (1 if r["swept"] else 0)
    stars = "⭐" * min(power, 3)
    return (
        f"🟢 <b>ХАРИД СИГНАЛИ — {r['sym']}</b> {stars}\n"
        f"Нарх: <b>{fp(r['price'])}</b>\n\n"
        f"Тренд: {tline} ({r['n_up']}/5)\n"
        f"Зона: АРЗОН {round(r['pct'])}%\n"
        f"Сабаб:\n{zones}\n"
        f"SSL супурилди: {'✅ ҳа' if r['swept'] else '— йўқ'}\n\n"
        f"Стоп: {fp(r['inval'])} (риск {r['risk_pct']:.1f}%)\n"
        f"Мўлжал (BSL): {fp(r['target'])} (+{r['profit_pct']:.1f}%)\n"
        f"Риск/фойда: 1:{r['rr']:.1f}\n\n"
        f"⚠️ Бу молиявий маслаҳат эмас — графикда тасдиқ кутинг."
    )


def send(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("\n" + text + "\n")          # токен йўқ — синов режими, экранга чиқаради
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
    except Exception as ex:
        logging.warning(f"Telegram хатоси: {ex}")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(sent):
    now = time.time()
    keep = {k: v for k, v in sent.items() if now - v < RESEND_H * 3600}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=1)


def scan_once(sent):
    for sym in SYMBOLS:
        try:
            r = analyze(sym)
            if r:
                key = sym + "|" + "|".join(sorted(f"{n}:{fp(b)}" for n, _, b in r["hits"]))
                if time.time() - sent.get(key, 0) > RESEND_H * 3600:
                    send(message(r))
                    sent[key] = time.time()
                    logging.info(f"Сигнал юборилди: {sym}")
                else:
                    logging.info(f"{sym}: сигнал аввал юборилган")
            else:
                logging.info(f"{sym}: сигнал йўқ")
        except Exception as ex:
            logging.warning(f"{sym}: {ex}")
        time.sleep(1)


def main():
    sent = load_state()
    if RUN_ONCE:                      # GitHub Actions режими
        scan_once(sent)
        save_state(sent)
        return
    send("🤖 <b>Сигнал бот ишга тушди</b>\nКузатилмоқда: " + ", ".join(SYMBOLS))
    while True:                       # доимий режим (компьютер/VPS)
        scan_once(sent)
        save_state(sent)
        time.sleep(SCAN_EVERY)


if __name__ == "__main__":
    main()
