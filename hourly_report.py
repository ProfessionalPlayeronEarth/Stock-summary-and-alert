#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stella 联合报告 — 每日两次（晨报 8:00 + 盘前 20:30 北京时间）

数据来源（免费、无需 API Key）：
  - 主源：CNBC 行情接口（一次批量请求覆盖全部标的）
  - 备源：Yahoo Finance chart API（CNBC 缺数据时逐个回退）

推送策略（2026-08-26 更新）：
  每次都推送，内容分两部分：
  ① 常规数据（每次都推）：美债收益率 1Y/2Y/5Y/10Y/30Y、美元指数、黄金、WTI 原油
  ② 异动部分（仅触发时推）：个股 ±3%、组合 ±1.5%、指数 ±1.5%
     未触发阈值的个股/指数不出现在报告中

推送（双通道，免费额度调度）：
  先走 PushPlus；额度用尽或失败时自动回退方糖 Server酱，两者都失败才算失败。
  环境变量（GitHub Secrets / 本地均可）：
    PUSHPLUS_TOKEN   PushPlus Token
    SERVERCHAN_KEY   方糖 Server酱 SendKey（SCT 开头）

排期（北京时间，每日两次）：
  08:00 晨报（全年）
  盘前报告：夏令时 20:30 / 冬令时 21:30 —— workflow 同时挂 12:30/13:30 UTC
  两个触发点，脚本按美国夏令时规则自动判断，错季的触发点静默跳过。

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
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "").strip()
DRY_RUN = "--dry" in sys.argv

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

YIELDS = [
    ("US1Y", "1Y"),
    ("US2Y", "2Y"),
    ("US5Y", "5Y"),
    ("US10Y", "10Y"),
    ("US30Y", "30Y"),
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
YIELD_ALERT_BP = 8.0   # 美债收益率日变动告警阈值 bp
INDEX_ALERT = 1.5      # 指数涨跌幅告警阈值 %
MACRO_ALERT = {        # 宏观品种涨跌幅告警阈值 %
    ".DXY": 1.0,   # 美元指数
    "@GC.1": 1.5,  # 黄金
    "@CL.1": 3.0,  # WTI 原油
}


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
    alert_hit = False

    def add(s=""):
        lines.append(s)

    add("### 联合报告")
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
            flag = ""
            if abs(bp) >= YIELD_ALERT_BP:
                flag = " ⚠️"
                alert_hit = True
            add("- {}：{:.3f}%（日变动 {:+.1f}bp{}，{}）".format(label, q["last"], bp, flag, q["time"]))
        except Exception as e:  # noqa: BLE001
            add("- {}：数据获取失败（{}）".format(label, e))
    if "10Y" in ys and "2Y" in ys:
        add("- 10Y-2Y 利差：{:+.1f}bp".format((ys["10Y"]["last"] - ys["2Y"]["last"]) * 100))
    add()
    add("---")

    # 三大指数 — 仅异动时展示
    idx_hits = []
    for sym, name in INDICES:
        try:
            q = quotes.get(sym)
            chg = q["change_pct"] or 0
            if abs(chg) >= INDEX_ALERT:
                idx_hits.append("- {}：{:,.2f}（{:+.2f}% ⚠️，{}）".format(
                    name, q["last"], chg, q["time"]))
                alert_hit = True
        except Exception as e:  # noqa: BLE001
            idx_hits.append("- {}：数据获取失败（{}）".format(name, e))
    if idx_hits:
        add()
        add("### 指数异动")
        add()
        for line in idx_hits:
            add(line)
        add()
        add("---")

    # 美元 / 黄金 / 原油
    add()
    add("### 美元 · 黄金 · 原油")
    add()
    for sym, name in MACRO:
        try:
            q = quotes.get(sym)
            chg = q["change_pct"] or 0
            th = MACRO_ALERT.get(sym, 2.0)
            flag = ""
            if abs(chg) >= th:
                flag = " ⚠️"
                alert_hit = True
            add("- {}：{:,.2f}（{:+.2f}%{}，{}）".format(name, q["last"], chg, flag, q["time"]))
        except Exception as e:  # noqa: BLE001
            add("- {}：数据获取失败（{}）".format(name, e))
    add()
    add("---")

    # 持仓动态 — 仅异动个股展示；组合加权照算（用于标题），仅异动时展示
    weighted = []
    stock_hits = []
    for sym, weight, cost in HOLDINGS:
        try:
            q = quotes.get(sym)
            chg = q["change_pct"] or 0
            pl = (q["last"] - cost) / cost * 100.0
            weighted.append((sym, weight, chg, pl))
            if abs(chg) >= STOCK_ALERT:
                unit = " 港元" if sym.endswith(".HK") else " 美元"
                stock_hits.append("- {}（{:.1f}%仓）：现价 {:,.2f}{}，今日 {:+.2f}% ⚠️（持仓 {:+.1f}%）".format(
                    sym, weight, q["last"], unit, chg, pl))
                alert_hit = True
        except Exception as e:  # noqa: BLE001
            stock_hits.append("- {}（{:.1f}%仓）：数据获取失败（{}）".format(sym, weight, e))
    if stock_hits:
        add()
        add("### 持仓异动")
        add()
        for line in stock_hits:
            add(line)
        add()
    # 组合加权 — 仅异动时展示
    pf = None
    pfl = None
    if weighted:
        total_w = sum(w for _, w, _, _ in weighted)
        pf = sum(w * c for _, w, c, _ in weighted) / total_w
        pfl = sum(w * l for _, w, _, l in weighted) / total_w
        if abs(pf) >= PORTFOLIO_ALERT:
            add()
            add("### 组合异动")
            add()
            add("- 当日加权：{:+.2f}% ⚠️".format(pf))
            add("- 持仓加权：{:+.2f}%".format(pfl))
            contrib = sorted(weighted, key=lambda x: abs(x[1] * x[2]), reverse=True)[:3]
            for sym, w, c, _ in contrib:
                add("- 贡献最大：{}（{:.1f}%仓 × {:+.2f}% = {:+.2f}pp）".format(
                    sym, w, c, w * c / 100))
            add()
            alert_hit = True
    add()
    add("---")

    # 数据缺口说明
    add()
    add("### 本报告不含（无免费数据接口）")
    add()
    add("- 下次 FOMC 概率、个股新闻与盘前异动解读")
    add("- 今晚财报与经济数据日程")
    add("- 霍尔木兹海峡通船数（由本地福福每日 16:00 监测，见单独推送）")
    add()
    add("---")
    add()
    add("数据来源：CNBC · Yahoo Finance（自动抓取，仅供参考，不构成投资建议）")

    title = "联合报告 " + now_bj_.strftime("%m-%d %H:%M")
    if pfl is not None:
        title += " 持仓{:+.1f}%".format(pfl)
    elif pf is not None:
        title += " 组合{:+.1f}%".format(pf)
    if alert_hit:
        title = "⚠️" + title
    title = title[:30]
    return title, "\n".join(lines), alert_hit


def _http_post_json(url, payload, timeout=15):
    data = json.dumps(payload).encode("utf-8")
    headers = {**UA, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _push_pushplus(title, content):
    """PushPlus，成功返回 True。额度用尽/失败返回 False 供上层回退。"""
    if not PUSHPLUS_TOKEN:
        print("[!] 未设置 PUSHPLUS_TOKEN")
        return False
    try:
        body = _http_post_json("https://www.pushplus.plus/send", {
            "token": PUSHPLUS_TOKEN,
            "title": title,
            "content": content,
            "template": "markdown",
        })
    except Exception as e:  # noqa: BLE001
        print("[!] PushPlus 请求异常：{}".format(e))
        return False
    if body.get("code") == 200:
        print("[+] PushPlus 推送成功：" + title)
        return True
    print("[!] PushPlus 失败（code={}，{}），准备回退方糖".format(
        body.get("code"), body.get("msg") or body.get("message") or ""))
    return False


def _push_serverchan(title, desp):
    """方糖 Server酱，成功返回 True。"""
    if not SERVERCHAN_KEY:
        print("[!] 未设置 SERVERCHAN_KEY")
        return False
    api = "https://sctapi.ftqq.com/{}.send".format(SERVERCHAN_KEY)
    data = urllib.parse.urlencode({"title": title[:32], "desp": desp}).encode("utf-8")
    req = urllib.request.Request(api, data=data, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print("[!] 方糖请求异常：{}".format(e))
        return False
    if body.get("code") == 0:
        print("[+] 方糖推送成功：" + title)
        return True
    print("[!] 方糖失败：" + json.dumps(body, ensure_ascii=False))
    return False


def push_wechat(title, desp):
    """双通道调度：PushPlus 优先，额度用尽/失败回退方糖，两者都失败才返回 False。"""
    if _push_pushplus(title, desp):
        return True
    return _push_serverchan(title, desp)


def premarket_gate():
    """盘前触发点 DST 自适应：BJ 20:30 与 21:30 两个 cron 都会触发，
    仅保留与当前美国夏令时状态匹配的那个，另一个静默跳过。
    早上 8:00 晨报与其他任意时刻（手动触发）不拦截。"""
    bj = now_bj()
    hhmm = bj.hour * 60 + bj.minute
    if not (20 * 60 + 15 <= hhmm <= 21 * 60 + 45):
        return True  # 非盘前窗口（晨报 / 手动触发），放行
    dst = _us_dst_active(_utcnow())
    expected = 20 * 60 + 30 if dst else 21 * 60 + 30  # 夏令时 20:30，冬令时 21:30
    if abs(hhmm - expected) > 45:
        print("[skip] 盘前触发点不匹配（当前{}令时，应在 {}:30 推送），本轮跳过".format(
            "夏" if dst else "冬", 20 if dst else 21))
        return False
    return True


def main():
    if not premarket_gate():
        return
    title, desp, alert = build_report()
    print("=" * 20 + " " + title + " " + "=" * 20)
    print(desp)
    print("=" * 52)
    if DRY_RUN:
        print("[dry-run] 未推送")
        return
    # 每次都推送（每日仅运行两次：晨报 + 盘前）
    ok = push_wechat(title, desp)
    if not ok:
        print("[!!] 双通道推送均失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
