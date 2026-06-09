# -*- coding: utf-8 -*-
"""
Market Peak Signals — BofA Exhibit 11 스타일 10-시그널 일일 모니터
- 7개 시그널: 무료 데이터 자동 계산 (FRED / Yahoo / CNN / multpl / BLS 폴백)
- 3개 시그널: BofA 독점 데이터 → manual_overrides.json 수동 관리
- 결과: docs/data/history.json 누적 → GitHub Pages 대시보드
- 알림: Telegram (HTML)
"""
import os, sys, json, math, time
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime("%Y-%m-%d")
ROOT = os.path.dirname(os.path.abspath(__file__))
HIST_PATH = os.path.join(ROOT, "docs", "data", "history.json")
OVR_PATH = os.path.join(ROOT, "manual_overrides.json")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
PEAK_AVG = 70  # 과거 피크 평균 트리거 비율 (%)


# ---------------------------------------------------------------- data utils
def http_get(url, retries=3, backoff=4, **kw):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=30, **kw)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)
        time.sleep(backoff * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def fred_series(series_id, years=11):
    """FRED 무키 CSV. (date, value) 리스트 반환."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    r = http_get(url)
    out = []
    cutoff = (datetime.now() - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    for line in r.text.strip().split("\n")[1:]:
        parts = line.split(",")
        if len(parts) < 2 or parts[1] in (".", ""):
            continue
        d, v = parts[0].strip(), parts[1].strip()
        if d >= cutoff:
            try:
                out.append((d, float(v)))
            except ValueError:
                pass
    if not out:
        raise RuntimeError(f"FRED {series_id}: empty")
    return out


def yahoo_closes(symbol, rng="1y"):
    """Yahoo chart API. (date, close) 리스트."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={rng}&interval=1d&events=div"
    r = http_get(url)
    res = r.json()["chart"]["result"][0]
    ts = res["timestamp"]
    cl = res["indicators"]["quote"][0]["close"]
    adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose") or cl
    out = []
    for t, c in zip(ts, adj):
        if c is not None:
            out.append((datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"), float(c)))
    return out


def zscore_last(values):
    n = len(values)
    mu = sum(values) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in values) / max(n - 1, 1))
    return (values[-1] - mu) / sd if sd > 0 else 0.0


def pct_rank_last(values):
    cur = values[-1]
    return sum(1 for v in values if v <= cur) / len(values)


# ---------------------------------------------------------------- signals
def sig_consumer_confidence():
    """S1 [프록시] UMich 소비자심리 6개월 내 최고치 > 90 (CB CCI>110 대용)."""
    data = fred_series("UMCSENT", years=2)
    recent = [v for d, v in data if d >= (datetime.now() - timedelta(days=183)).strftime("%Y-%m-%d")]
    mx = max(recent) if recent else data[-1][1]
    return mx > 90, f"6m max {mx:.1f} (기준>90)"


def sig_stocks_higher():
    """S2 [프록시] CNN Fear&Greed 6개월 내 75 이상(Extreme Greed) 도달 여부."""
    r = http_get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata")
    pts = r.json()["fear_and_greed_historical"]["data"]
    cut = (datetime.now(timezone.utc) - timedelta(days=183)).timestamp() * 1000
    vals = [p["y"] for p in pts if p["x"] >= cut]
    mx = max(vals)
    return mx >= 75, f"6m max F&G {mx:.0f} (기준≥75)"


def _cpi_monthly():
    """CPI 월간 (12년). FRED 우선 → BLS 2회 분할 폴백."""
    try:
        return {d[:7]: v for d, v in fred_series("CPIAUCSL", years=12)}
    except Exception:
        y = datetime.now().year
        cpi = {}
        for sy, ey in [(y - 11, y - 6), (y - 5, y)]:
            b = requests.post("https://api.bls.gov/publicAPI/v1/timeseries/data/",
                              json={"seriesid": ["CUUR0000SA0"],
                                    "startyear": str(sy), "endyear": str(ey)},
                              timeout=30).json()
            for it in b["Results"]["series"][0]["data"]:
                if it["period"].startswith("M") and it["period"] != "M13":
                    try:
                        cpi[f"{it['year']}-{it['period'][1:]}"] = float(it["value"])
                    except ValueError:
                        pass
        return cpi


def sig_pe_cpi():
    """S6 (S&P500 trailing PE + CPI YoY)의 10년 Z-score > 1.
    PE 시계열 재구성: Yahoo 월간 SPX ÷ EPS(Shiller 히스토리 + multpl 현재 PE 앵커 로그선형 보간)."""
    import re
    # (1) 현재 PE 앵커 (multpl 최신 행)
    r = http_get("https://www.multpl.com/s-p-500-pe-ratio/table/by-month")
    rows = re.findall(
        r"<td>\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s*</td>\s*<td>\s*(?:<abbr[^>]*>[^<]*</abbr>\s*)?([\d.]+)",
        r.text)
    if not rows:
        raise RuntimeError("multpl parse: 0 rows")
    pe_now = float(rows[0][1])
    # (2) Yahoo 월간 SPX 종가 (11년)
    px = yahoo_closes("%5EGSPC", "11y")
    monthly = {}
    for d, v in px:
        monthly[d[:7]] = v  # 월말 값으로 갱신
    months = sorted(monthly)
    # (3) EPS: Shiller(datahub) 히스토리 + 현재 implied EPS 보간
    import csv, io
    rr = http_get("https://datahub.io/core/s-and-p-500/r/data.csv")
    eps_hist = {}
    for row in csv.DictReader(io.StringIO(rr.text)):
        try:
            e = float(row.get("Earnings") or 0)
        except ValueError:
            continue
        if e > 0:
            eps_hist[row["Date"][:7]] = e
    last_hist_m = max(eps_hist)
    cur_m = months[-1]
    eps_now = monthly[cur_m] / pe_now
    # 로그선형 보간 (last_hist_m → cur_m)
    def midx(m): return int(m[:4]) * 12 + int(m[5:7])
    span = midx(cur_m) - midx(last_hist_m)
    eps = dict(eps_hist)
    if span > 0:
        g = (math.log(eps_now) - math.log(eps_hist[last_hist_m])) / span
        for m in months:
            k = midx(m) - midx(last_hist_m)
            if 0 < k <= span:
                eps[m] = math.exp(math.log(eps_hist[last_hist_m]) + g * k)
    # (4) CPI YoY
    cpi = _cpi_monthly()
    series = []
    for m in months[-120:]:
        prev = f"{int(m[:4])-1}{m[4:]}"
        if m in eps and m in cpi and prev in cpi:
            yoy = (cpi[m] / cpi[prev] - 1) * 100
            series.append(monthly[m] / eps[m] + yoy)
    if len(series) < 60:
        raise RuntimeError(f"pe_cpi series too short: {len(series)}")
    z = zscore_last(series)
    return z > 1, f"10y Z {z:+.2f} (PE {pe_now:.1f}, 기준>1)"


def sig_low_pe_underperf():
    """S7 Low PE(가치, IVE)가 High PE(성장, IVW) 대비 최근 6개월 -2.5%p 이상 부진."""
    ive = yahoo_closes("IVE", "1y")
    ivw = yahoo_closes("IVW", "1y")
    def ret6(series):
        cut = (datetime.now() - timedelta(days=183)).strftime("%Y-%m-%d")
        win = [v for d, v in series if d >= cut]
        return (win[-1] / win[0] - 1) * 100
    rv, rg = ret6(ive), ret6(ivw)
    gap = rv - rg
    return gap < -2.5, f"Value-Growth 6m {gap:+.1f}%p (기준<-2.5)"


def sig_yield_curve():
    """S8 최근 6개월 내 금리역전 발생 여부 (10Y-2Y, 폴백 10Y-3M)."""
    try:
        data = fred_series("T10Y2Y", years=1)
        label = "10Y-2Y"
    except Exception:
        t10 = dict(yahoo_closes("%5ETNX", "6mo"))
        t3m = dict(yahoo_closes("%5EIRX", "6mo"))
        common = sorted(set(t10) & set(t3m))
        data = [(d, t10[d] - t3m[d]) for d in common]
        label = "10Y-3M(폴백)"
    cut = (datetime.now() - timedelta(days=183)).strftime("%Y-%m-%d")
    win = [v for d, v in data if d >= cut]
    mn = min(win)
    return mn < 0, f"{label} 6m min {mn:+.2f}%p (역전<0)"


def sig_credit_stress():
    """S9 [프록시] HY OAS 10년 백분위 ≤ 25% (스프레드 과압축 = BofA CSI<0.25 대용)."""
    data = fred_series("BAMLH0A0HYM2", years=10)
    vals = [v for _, v in data]
    pr = pct_rank_last(vals)
    return pr <= 0.25, f"HY OAS {vals[-1]:.2f}%, 10y 백분위 {pr*100:.0f}% (기준≤25%)"


def sig_sloos():
    """S10 SLOOS 대출태도 순강화(>0) 여부 (분기, FRED DRTSCILM)."""
    data = fred_series("DRTSCILM", years=3)
    d, v = data[-1]
    return v > 0, f"C&I 순강화 {v:+.1f}% ({d})"


AUTO_SIGNALS = [
    ("consumer_confidence", "소비자신뢰 고점권", "Sentiment", sig_consumer_confidence, True),
    ("stocks_higher",       "주가상승 기대 과열", "Sentiment", sig_stocks_higher, True),
    ("pe_cpi",              "PE+CPI 10y Z>1",    "Valuation", sig_pe_cpi, False),
    ("low_pe_underperf",    "저PE 6m 언더퍼폼",   "Valuation", sig_low_pe_underperf, False),
    ("yield_curve",         "금리역전(6m 내)",    "Macro",     sig_yield_curve, False),
    ("credit_stress",       "크레딧 과압축",      "Macro",     sig_credit_stress, True),
    ("sloos",               "대출태도 긴축(SLOOS)","Macro",    sig_sloos, False),
]
MANUAL_SIGNALS = [
    ("sell_side", "BofA Sell Side 'Sell'", "Sentiment"),
    ("ltg",       "LTG 5y Z>1",            "Sentiment"),
    ("mna",       "M&A 10y Z>1",           "Sentiment"),
]


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def main():
    tg_token = os.environ.get("TELEGRAM_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    cfg = load_json(os.path.join(ROOT, "config.json"), {})
    tg_token = tg_token or cfg.get("telegram_token", "")
    tg_chat = tg_chat or cfg.get("telegram_chat_id", "")

    history = load_json(HIST_PATH, [])
    last = history[-1] if history else {"signals": {}}
    overrides = load_json(OVR_PATH, {})

    signals = {}
    errors = []

    for sid, name, cat, fn, is_proxy in AUTO_SIGNALS:
        try:
            trig, val = fn()
            signals[sid] = {"name": name, "cat": cat, "t": bool(trig), "v": val,
                            "proxy": is_proxy, "src": "auto"}
        except Exception as e:
            prev = last["signals"].get(sid)
            errors.append(f"{sid}: {e}")
            if prev:
                signals[sid] = {**prev, "src": "carry"}
            else:
                signals[sid] = {"name": name, "cat": cat, "t": None, "v": "n/a",
                                "proxy": is_proxy, "src": "fail"}

    for sid, name, cat in MANUAL_SIGNALS:
        o = overrides.get(sid, {})
        signals[sid] = {"name": name, "cat": cat,
                        "t": o.get("triggered"), "v": f"수동({o.get('as_of','-')})",
                        "proxy": False, "src": "manual"}

    known = [s for s in signals.values() if s["t"] is not None]
    n_trig = sum(1 for s in known if s["t"])
    pct = round(100 * n_trig / len(known)) if known else 0

    # S&P 500 종가
    try:
        spx = yahoo_closes("%5EGSPC", "5d")[-1][1]
    except Exception:
        spx = last.get("sp500")

    entry = {"date": TODAY, "pct": pct, "n_trig": n_trig, "n_total": len(signals),
             "n_known": len(known), "sp500": round(spx, 2) if spx else None,
             "signals": signals}
    history = [h for h in history if h["date"] != TODAY] + [entry]
    history = history[-730:]
    os.makedirs(os.path.dirname(HIST_PATH), exist_ok=True)
    with open(HIST_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)

    # ------------------------------------------------------------ telegram
    if pct >= PEAK_AVG:
        level, head = "🔴", "피크 경보 — 과거 고점 평균 도달"
    elif pct >= 50:
        level, head = "🟠", "주의 — 시그널 누적 중"
    elif pct >= 30:
        level, head = "🟡", "관찰 — 일부 시그널 점등"
    else:
        level, head = "🟢", "안정"

    prev_pct = history[-2]["pct"] if len(history) >= 2 else None
    delta = f" (전일 {prev_pct}%→)" if prev_pct is not None and prev_pct != pct else ""

    mark = lambda t: "✅" if t is True else ("➖" if t is False else "❔")
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lines = [f"{level} <b>마켓 피크 시그널 {pct}%</b>{delta}",
             f"<i>{esc(head)} · 트리거 {n_trig}/{len(signals)} (판정가능 {len(known)})</i>",
             f"S&amp;P 500: {entry['sp500']:,}" if entry["sp500"] else "", ""]
    order = ["consumer_confidence", "stocks_higher", "sell_side", "ltg", "mna",
             "pe_cpi", "low_pe_underperf", "yield_curve", "credit_stress", "sloos"]
    for sid in order:
        s = signals[sid]
        px = "≈" if s.get("proxy") else ""
        lines.append(f"{mark(s['t'])} {px}{esc(s['name'])} — {esc(s['v'])}")
    lines += ["", "기준: 과거 7회 고점 평균 트리거 70%",
              "📊 대시보드: https://jinhae8971.github.io/market-peak-signals/"]
    if errors:
        lines.append(f"⚠️ 수집실패 {len(errors)}건(전일값 유지)")
    msg = "\n".join(l for l in lines if l is not None)

    if tg_token and tg_chat:
        api = f"https://api.telegram.org/bot{tg_token}/sendMessage"
        r = requests.post(api, json={"chat_id": tg_chat, "text": msg,
                                     "parse_mode": "HTML"}, timeout=20)
        if r.status_code != 200:
            # HTML 파싱 오류 등 → 플레인텍스트 폴백
            import re as _re
            plain = _re.sub(r"<[^>]+>", "", msg).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            r = requests.post(api, json={"chat_id": tg_chat, "text": plain}, timeout=20)
        r.raise_for_status()
        print("Telegram sent.")
    else:
        print("Telegram skipped (no credentials).")
    print(msg)
    if errors:
        print("ERRORS:", *errors, sep="\n  ")


if __name__ == "__main__":
    main()
