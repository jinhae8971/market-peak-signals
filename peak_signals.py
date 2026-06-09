# -*- coding: utf-8 -*-
"""
Market Peak Signals Monitor
BofA Exhibit 11 기반 10개 시장 피크 시그널 일일 점검
- 5개 자동 계산 (FRED / yfinance / multpl)
- 5개 수동 관리 (manual_signals.json, 월 1회 갱신)
출력: Telegram 알림 + docs/data/history.json (GitHub Pages 대시보드)
"""
import os
import io
import json
import math
import datetime as dt

import requests
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "docs", "data")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
MANUAL_PATH = os.path.join(BASE_DIR, "manual_signals.json")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) peak-signals/1.0"}
PEAK_AVG = 70  # 과거 피크 평균 트리거 비율 (%)


# ---------------------------------------------------------------- utils
def load_config() -> dict:
    cfg = {
        "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    }
    config_path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if not cfg.get(k):
                    cfg[k] = v
    return cfg


def fred_series(series_id: str, years: int = 12) -> pd.Series:
    """FRED CSV 엔드포인트 (API key 불필요), 503 대비 지수 백오프 재시도"""
    import time
    start = (dt.date.today() - dt.timedelta(days=365 * years)).isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}"
    last_err = None
    for attempt in range(4):
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            break
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt * 3)
    else:
        raise last_err
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna().set_index("date")["value"]


def yahoo_closes(symbol: str, months: int = 8) -> pd.Series:
    import yfinance as yf
    df = yf.download(symbol, period=f"{months}mo", interval="1d",
                     progress=False, auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"yfinance empty: {symbol}")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    return close.dropna()


def multpl_pe_history() -> pd.Series:
    """S&P 500 trailing PE 월별 히스토리 (multpl.com)"""
    url = "https://www.multpl.com/s-p-500-pe-ratio/table/by-month"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    tables = pd.read_html(io.StringIO(r.text))
    df = tables[0]
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["value"] = (df["value"].astype(str)
                   .str.extract(r"([\d.]+)")[0].astype(float))
    return df.dropna().set_index("date")["value"].sort_index()


# ---------------------------------------------------------------- signals
def sig_pe_cpi_z() -> dict:
    """S6: 10yr Z score of (trailing PE + YoY CPI) > 1"""
    pe = multpl_pe_history()
    cpi = fred_series("CPIAUCSL", years=12)
    cpi_yoy = cpi.pct_change(12) * 100
    pe_m = pe.resample("MS").last()
    combo = (pe_m + cpi_yoy.reindex(pe_m.index, method="nearest")).dropna()
    window = combo[combo.index >= combo.index.max() - pd.DateOffset(years=10)]
    z = (window.iloc[-1] - window.mean()) / window.std()
    return {
        "triggered": bool(z > 1),
        "value": f"Z={z:.2f} (PE {pe_m.iloc[-1]:.1f} + CPI {cpi_yoy.dropna().iloc[-1]:.1f}%)",
    }


def sig_value_vs_growth() -> dict:
    """S7: Low PE(Value)가 High PE(Growth) 대비 6m -2.5ppt 언더퍼폼 (IVE vs IVW 프록시)"""
    ive = yahoo_closes("IVE")
    ivw = yahoo_closes("IVW")
    days = 126  # ~6개월 거래일
    r_v = ive.iloc[-1] / ive.iloc[-min(days, len(ive))] - 1
    r_g = ivw.iloc[-1] / ivw.iloc[-min(days, len(ivw))] - 1
    spread = (r_v - r_g) * 100
    return {
        "triggered": bool(spread < -2.5),
        "value": f"Value-Growth 6m {spread:+.1f}ppt",
    }


def sig_yield_curve() -> dict:
    """S8: 최근 6개월 내 10Y-3M 역전 발생 여부"""
    s = fred_series("T10Y3M", years=2)
    recent = s[s.index >= s.index.max() - pd.DateOffset(months=6)]
    inverted = bool((recent < 0).any())
    return {
        "triggered": inverted,
        "value": f"10Y-3M now {s.iloc[-1]:+.2f}%, 6m min {recent.min():+.2f}%",
    }


def sig_credit_stress() -> dict:
    """S9: Credit Stress 프록시 — HY OAS 10년 백분위 < 0.25 (스프레드 과열 압축)"""
    s = fred_series("BAMLH0A0HYM2", years=10)
    pct = float((s < s.iloc[-1]).mean())
    return {
        "triggered": bool(pct < 0.25),
        "value": f"HY OAS {s.iloc[-1]:.2f}%, 10y pct {pct:.2f}",
    }


def sig_sloos() -> dict:
    """S10: SLOOS 대출태도 긴축 (Net % > 0, 분기)"""
    s = fred_series("DRTSCILM", years=5)
    latest = float(s.iloc[-1])
    return {
        "triggered": bool(latest > 0),
        "value": f"Net tightening {latest:+.1f}% ({s.index[-1]:%Y-%m})",
    }


AUTO_SIGNALS = [
    ("S6", "Valuation", "PE+CPI 10y Z-score > 1", sig_pe_cpi_z),
    ("S7", "Valuation", "Low PE 6m -2.5ppt 언더퍼폼", sig_value_vs_growth),
    ("S8", "Macro", "금리역전 (최근 6m)", sig_yield_curve),
    ("S9", "Macro", "Credit Stress < 0.25", sig_credit_stress),
    ("S10", "Macro", "SLOOS 대출태도 긴축", sig_sloos),
]

MANUAL_META = [
    ("S1", "Sentiment", "CB 소비자신뢰지수 > 110"),
    ("S2", "Sentiment", "CB 주가상승 기대 Net% > 20"),
    ("S3", "Sentiment", "BofA Sell Side Indicator 'Sell'"),
    ("S4", "Sentiment", "S&P500 LTG 5y Z > 1"),
    ("S5", "Sentiment", "M&A 건수 10y Z > 1"),
]


def evaluate() -> list:
    results = []
    # 수동 시그널
    manual = {}
    if os.path.exists(MANUAL_PATH):
        with open(MANUAL_PATH, "r", encoding="utf-8") as f:
            manual = json.load(f)
    for sid, cat, name in MANUAL_META:
        m = manual.get(sid, {})
        results.append({
            "id": sid, "category": cat, "name": name, "mode": "manual",
            "triggered": bool(m.get("triggered", False)),
            "value": m.get("note", "") + (f" (as of {m.get('as_of','?')})" if m.get("as_of") else ""),
            "error": False,
        })
    # 자동 시그널
    for sid, cat, name, fn in AUTO_SIGNALS:
        try:
            r = fn()
            results.append({"id": sid, "category": cat, "name": name,
                            "mode": "auto", "triggered": r["triggered"],
                            "value": r["value"], "error": False})
        except Exception as e:  # 개별 실패가 전체를 막지 않도록
            results.append({"id": sid, "category": cat, "name": name,
                            "mode": "auto", "triggered": False,
                            "value": f"ERROR: {type(e).__name__}: {e}", "error": True})
    return results


# ---------------------------------------------------------------- outputs
def update_history(date_str: str, pct: int, results: list) -> list:
    hist = []
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            hist = json.load(f)
    hist = [h for h in hist if h["date"] != date_str]
    hist.append({
        "date": date_str,
        "pct_triggered": pct,
        "signals": {r["id"]: r["triggered"] for r in results},
        "details": {r["id"]: r["value"] for r in results},
    })
    hist.sort(key=lambda h: h["date"])
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)
    return hist


def build_message(date_str: str, results: list, pct: int) -> str:
    n_trig = sum(r["triggered"] for r in results)
    n_err = sum(r["error"] for r in results)
    if pct >= PEAK_AVG:
        verdict = "🔴 <b>피크 경보</b> — 과거 피크 평균(70%) 도달"
    elif pct >= 50:
        verdict = "🟠 <b>주의</b> — 피크 평균에 근접 중"
    else:
        verdict = "🟢 <b>안정</b> — 피크 평균 대비 여유"
    bar_n = round(pct / 10)
    gauge = "█" * bar_n + "░" * (10 - bar_n)

    lines = [
        f"📊 <b>Market Peak Signals</b> | {date_str}",
        "",
        f"{gauge} <b>{pct}%</b> ({n_trig}/10 triggered)",
        verdict,
        "",
    ]
    cat_prev = None
    for r in results:
        if r["category"] != cat_prev:
            lines.append(f"<b>[{r['category']}]</b>")
            cat_prev = r["category"]
        mark = "🔺" if r["triggered"] else ("⚠️" if r["error"] else "▫️")
        tag = "" if r["mode"] == "auto" else " ✍️"
        lines.append(f"{mark} {r['name']}{tag}")
        if r["value"] and not r["error"]:
            lines.append(f"    <i>{r['value']}</i>")
        elif r["error"]:
            lines.append(f"    <i>{r['value'][:80]}</i>")
    lines += [
        "",
        f"기준: BofA Exhibit 11 (피크 평균 ~{PEAK_AVG}%)",
        "✍️ = 수동 시그널 (manual_signals.json)",
    ]
    if n_err:
        lines.append(f"⚠️ 자동 시그널 {n_err}건 수집 실패")
    lines.append("📈 대시보드: https://jinhae8971.github.io/market-peak-signals/")
    return "\n".join(lines)


def send_telegram(msg: str, token: str, chat_id: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={
        "chat_id": chat_id, "text": msg,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }, timeout=20)
    r.raise_for_status()


def main():
    cfg = load_config()
    today = dt.date.today().isoformat()
    results = evaluate()
    pct = round(sum(r["triggered"] for r in results) / 10 * 100)
    update_history(today, pct, results)
    msg = build_message(today, results, pct)
    print(msg.replace("<b>", "").replace("</b>", "")
             .replace("<i>", "").replace("</i>", ""))
    if cfg["telegram_token"] and cfg["telegram_chat_id"]:
        send_telegram(msg, cfg["telegram_token"], cfg["telegram_chat_id"])
        print("\n[OK] Telegram sent")
    else:
        print("\n[SKIP] Telegram credentials not set")


if __name__ == "__main__":
    main()
