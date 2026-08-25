#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stella 整点联合报告 — 每小时运行一次（GitHub Actions / 本地均可）

数据来源（免费、无需 API Key）：
  - 主源：CNBC 行情接口（一次批量请求覆盖全部标的）
  - 备源：Yahoo Finance chart API（CNBC 缺数据时逐个回退）

推送：PushPlus，一条消息合并完整版报告。
环境变量：
  PUSHPLUS_TOKEN  PushPlus Token（GitHub Secret，勿写入代码）
用法：
  python hourly_report.py --dry   # 只打印，不推送
  python hourly_report.py         # 打印并推送
"""

import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
DRY_RUN = "--dry" in sys.argv

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

YIELDS = [
    ("US10Y", "10Y"),
    ("US2Y", "2Y"),
]
INDICES = [
    (".SPX", "标普500"),
    (".DJI", "道琼斯"),
    (".IXIC", "纳斯达克"),
]
MACRO = [
    (".DXY", "美元指数"),
    ("@GC.1", "黄金COMEX"),
    ("@CL.1", "WTI原油"),
]
HOLDINGS = [
    # (ticker, weight%, avg_cost) — 2026-08-24 更新
    ("GLD", 39.06, 425.91),
    ("SPCX", 17.49, 155.02),
    ("CEG", 5.78, 289.67),
    ("WEAT", 5.42, 25.71),  # Teucrium Wheat Fund 小麦 ETF（券商简写为 WAT）
    ("HLTH", 3.15, 29.22),
    ("2837.HK", 2.30, 7.803),
    ("MU", 1.92, 945.39),
    ("AVGO", 0.77, 425.68),
    ("IBKR", 0.75, 63.20),
    ("VRT", 0.54, 309.44),
    ("MRVL", 0.48, 236.01),
    ("NVDA", 0.45, 208.58),
    ("SKHY", 0.33, 160.76),
]

STOCK_ALERT = 3.0      # 个股涨跌幅告警阈值 %
PORTFOLIO_ALERT = 1.5  # 组合日盈亏告警阈值 %


# ---------------- 时间（不依赖 tzdata，兼容 Windows / Linux） ----------------

def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _nth_weekday(year, month, weekday, n):
    d = datetime.date(year, month, 1)
    count = 0
    while True:
        if d.weekday() == weekday:
            count += 1
            if count == n:
                return d
        d += datetime.timedelta(days=1)


def _us_dst_active(u):
    """美国夏令时：3 月第二个周日至 11 月第一个周日。"""
    start = datetime.datetime.combine(
        _nth_weekday(u.year, 3, 6, 2), datetime.time(7), tzinfo=datetime.timezone.utc
    )
    end = datetime.datetime.combine(
        _nth_weekday(u.year, 11, 6, 1), datetime.time(6), tzinfo=datetime.timezone.utc
    )
    return start <= u < end


def now_et():
    u = _utcnow()
    return u + datetime.timedelta(hours=-4 if _us_dst_active(u) else -5)


def now_bj():
    return _utcnow() + datetime.timedelta(hours=8)


def market_status(now_et_):
    if now_et_.weekday() >= 5:
        return "休市（周末）"
    h = now_et_.hour + now_et_.minute / 60.0
    if h < 4:
        return "休市"
    if h < 9.5:
        return "盘前"
    if h < 16:
        return "交易中"
    if h < 20:
        return "盘后"
    return "休市"


# ---------------- HTTP ----------------

def http_get(url, timeout=15):
    last_err = None
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5)
    raise last_err


def _num(s):
    """'4,683.25' / '+0.55%' / '4.734%' → float；无效返回 None。"""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("%", "").replace("+", "").strip()
    if s in ("", "-", "N/A", "--", "NA"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------- 数据源 ----------------

def cnbc_batch(symbols):
    """CNBC 批量行情。返回 {symbol: {name,last,change,change_pct,time}}。"""
    url = (
        "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
        "?symbols=" + urllib.parse.quote("|".join(symbols))
        + "&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json"
    )
    data = json.loads(http_get(url))
    out = {}
    for q in data.get("FormattedQuoteResult", {}).get("FormattedQuote", []):
        last = _num(q.get("last"))
        if last is None:
            continue
        out[q.get("symbol", "")] = {
            "name": q.get("name") or "",
            "last": last,
            "change": _num(q.get("change")),
            "change_pct": _num(q.get("change_pct")),
            "time": q.get("last_timedate") or "",
        }
    return out


def yahoo_quote(symbol):
    """Yahoo 备源。返回同结构 dict，失败抛异常。"""
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(symbol) + "?interval=1d&range=5d"
    )
    data = json.loads(http_get(url))
    result = data["chart"]["result"][0]
    meta = result["meta"]
    price = meta.get("regularMarketPrice")
    closes = [
        c for c in (result.get("indicators", {}).get("quote", [{}])[0].get("close") or [])
        if c is not None
    ]
    if price is None and not closes:
        raise ValueError("no price data")
    if price is None:
        price = closes[-1]
    prev = None
    if closes:
        if len(closes) >= 2 and abs(closes[-1] - price) < 1e-9:
            prev = closes[-2]
        else:
            prev = closes[-1]
    if not prev:
        raise ValueError("no previous close")
    ts = meta.get("regularMarketTime")
    tstr = ""
    if ts:
        u = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        tstr = (u + datetime.timedelta(hours=-4 if _us_dst_active(u) else -5)).strftime(
            "%m-%d %H:%M 美东")
    return {
        "name": meta.get("shortName") or symbol,
        "last": price,
        "change": price - prev,
        "change_pct": (price - prev) / prev * 100.0,
        "time": tstr,
    }


class Quotes:
    """CNBC 批量为主，缺谁用 Yahoo 单独补。"""

    def __init__(self, symbols):
        try:
            self.cache = cnbc_batch(symbols)
        except Exception as e:  # noqa: BLE001
            print("[warn] CNBC batch failed: {}".format(e))
            self.cache = {}

    def get(self, symbol):
        if symbol in self.cache:
            return self.cache[symbol]
        try:
            q = yahoo_quote(symbol)
            self.cache[symbol] = q
            return q
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(str(e))


# ---------------- 报告 ----------------

def build_report():
    now_et_ = now_et()
    now_bj_ = now_bj()
    all_symbols = (
        [s for s, _ in YIELDS] + [s for s, _ in INDICES]
        + [s for s, _ in MACRO] + [s for s, _, _ in HOLDINGS]
    )
    quotes = Quotes(all_symbols)
    lines = []

    def add(s=""):
        lines.append(s)

    add("### 整点联合报告")
    add()
    add("- 北京时间：{}".format(now_bj_.strftime("%Y-%m-%d %H:%M")))
    add("- 美东时间：{}（{}）".format(now_et_.strftime("%Y-%m-%d %H:%M"), market_status(now_et_)))
    add()
    add("---")

    # 美债收益率
    add()
    add("### 美债收益率")
    add()
    ys = {}
    for sym, label in YIELDS:
        try:
            q = quotes.get(sym)
            ys[label] = q
            bp = (q["change"] or 0) * 100
            add("- {}：{:.3f}%（日变动 {:+.1f}bp，{}）".format(label, q["last"], bp, q["time"]))
        except Exception as e:  # noqa: BLE001
            add("- {}：数据获取失败（{}）".format(label, e))
    if "10Y" in ys and "2Y" in ys:
        add("- 10Y-2Y 利差：{:+.1f}bp".format((ys["10Y"]["last"] - ys["2Y"]["last"]) * 100))
    add()
    add("---")

    # 三大指数
    add()
    add("### 美股三大指数")
    add()
    for sym, name in INDICES:
        try:
            q = quotes.get(sym)
            add("- {}：{:,.2f}（{:+.2f}%，{}）".format(name, q["last"], q["change_pct"] or 0, q["time"]))
        except Exception as e:  # noqa: BLE001
            add("- {}：数据获取失败（{}）".format(name, e))
    add()
    add("---")

    # 美元 / 黄金 / 原油
    add()
    add("### 美元 · 黄金 · 原油")
    add()
    for sym, name in MACRO:
        try:
            q = quotes.get(sym)
            add("- {}：{:,.2f}（{:+.2f}%，{}）".format(name, q["last"], q["change_pct"] or 0, q["time"]))
        except Exception as e:  # noqa: BLE001
            add("- {}：数据获取失败（{}）".format(name, e))
    add()
    add("---")

    # 持仓动态
    add()
    add("### 持仓动态（13 只）")
    add()
    weighted = []
    for sym, weight, cost in HOLDINGS:
        try:
            q = quotes.get(sym)
            chg = q["change_pct"] or 0
            flag = " ⚠️" if abs(chg) >= STOCK_ALERT else ""
            pl = (q["last"] - cost) / cost * 100.0
            unit = " 港元" if sym.endswith(".HK") else " 美元"
            add("- {}（{:.1f}%仓）：现价 {:,.2f}{}，今日 {:+.2f}%{}（持仓 {:+.1f}%）".format(
                sym, weight, q["last"], unit, chg, flag, pl))
            weighted.append((sym, weight, chg, pl))
        except Exception as e:  # noqa: BLE001
            add("- {}（{:.1f}%仓）：数据获取失败（{}）".format(sym, weight, e))
    add()
    pf = None
    pfl = None
    if weighted:
        total_w = sum(w for _, w, _, _ in weighted)
        pf = sum(w * c for _, w, c, _ in weighted) / total_w
        pfl = sum(w * l for _, w, _, l in weighted) / total_w
        flag = " ⚠️" if abs(pf) >= PORTFOLIO_ALERT else ""
        add("### 组合")
        add()
        add("- 当日加权：{:+.2f}%{}".format(pf, flag))
        add("- 持仓加权：{:+.2f}%".format(pfl))
        contrib = sorted(weighted, key=lambda x: abs(x[1] * x[2]), reverse=True)[:3]
        for sym, w, c, _ in contrib:
            add("- 贡献最大：{}（{:.1f}%仓 × {:+.2f}% = {:+.2f}pp）".format(sym, w, c, w * c / 100))
    add()
    add("---")

    # 数据缺口说明
    add()
    add("### 本报告不含（无免费数据接口）")
    add()
    add("- 9 月 FOMC 概率、个股新闻与盘前异动解读")
    add("- 今晚财报与经济数据日程")
    add("- 霍尔木兹海峡通船数（由本地福福每日 16:00 监测，见单独推送）")
    add()
    add("---")
    add()
    add("数据来源：CNBC · Yahoo Finance（自动抓取，仅供参考，不构成投资建议）")

    title = "整点联合报告 " + now_bj_.strftime("%H:%M")
    if pfl is not None:
        title += " 持仓{:+.1f}%".format(pfl)
    elif pf is not None:
        title += " 组合{:+.1f}%".format(pf)
    title = title[:30]
    return title, "\n".join(lines)


def push_wechat(title, desp):
    if not PUSHPLUS_TOKEN:
        print("[!] 未设置 PUSHPLUS_TOKEN，跳过推送")
        return False
    api = "https://www.pushplus.plus/send"
    payload = json.dumps({
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": desp,
        "template": "markdown",
    }).encode("utf-8")
    headers = {**UA, "Content-Type": "application/json"}
    req = urllib.request.Request(api, data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("code") == 200:
        print("[+] 推送成功：" + title)
        return True
    print("[!] 推送失败：" + json.dumps(body, ensure_ascii=False))
    return False


def main():
    title, desp = build_report()
    print("=" * 20 + " " + title + " " + "=" * 20)
    print(desp)
    print("=" * 52)
    if DRY_RUN:
        print("[dry-run] 未推送")
        return
    push_wechat(title, desp)


if __name__ == "__main__":
    main()
