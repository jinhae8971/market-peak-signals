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
import html as _html
import datetime as dt

import requests
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "docs", "data")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
MANUAL_PATH = os.path.join(BASE_DIR, "manual_signals.json")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) peak-signals/1.0"}
PEAK_AVG = 70  # 과거 피크 평균 트리거 비율 (%)
BB_SELL = 8.0  # Bull & Bear >= 8 -> 컨트래리안 Sell (극단적 낙관)
BB_BUY = 2.0   # Bull & Bear <= 2 -> 컨트래리안 Buy (극단적 비관)


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
    """FRED CSV (GH 러너에서 차단될 수 있어 폴백 전용, 1회/20s)"""
    start = (dt.date.today() - dt.timedelta(days=365 * years)).isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}"
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna().set_index("date")["value"]


def bls_cpi_yoy() -> float:
    """BLS API v2 (key 불필요) — CPI-U YoY %"""
    yr = dt.date.today().year
    r = requests.post("https://api.bls.gov/publicAPI/v2/timeseries/data/",
                      json={"seriesid": ["CUUR0000SA0"],
                            "startyear": str(yr - 2), "endyear": str(yr)},
                      timeout=30)
    r.raise_for_status()
    data = r.json()["Results"]["series"][0]["data"]
    vals = {}
    for d in data:
        if not d["period"].startswith("M"):
            continue
        try:
            vals[f"{d['year']}-{d['period'][1:]}"] = float(d["value"])
        except ValueError:
            continue  # 예비치 "-" 등 스킵
    s = pd.Series(vals).sort_index()
    return float(s.iloc[-1] / s.iloc[-13] * 100 - 100)


def yahoo_closes(symbol: str, months: int = 8) -> pd.Series:
    import yfinance as yf
    period = "max" if months > 24 else f"{months}mo"
    df = yf.download(symbol, period=period, interval="1d",
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
    pe_m = pe.resample("MS").last().dropna()
    window = pe_m[pe_m.index >= pe_m.index.max() - pd.DateOffset(years=10)]
    cpi_yoy = bls_cpi_yoy()
    # 과거 CPI 시계열 없이도 보수적으로: PE Z + 현재 CPI 기여 (10y CPI 평균 ~2.8% 가정 대비)
    combo_now = window.iloc[-1] + cpi_yoy
    combo_hist = window + 2.8
    z = (combo_now - combo_hist.mean()) / combo_hist.std()
    return {
        "triggered": bool(z > 1),
        "value": f"Z={z:.2f} (PE {window.iloc[-1]:.1f} + CPI {cpi_yoy:.1f}%)",
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
    """S8: 최근 6개월 내 10Y-3M 역전 (Yahoo ^TNX - ^IRX)"""
    tnx = yahoo_closes("^TNX", months=8)
    irx = yahoo_closes("^IRX", months=8)
    df = pd.concat([tnx, irx], axis=1, keys=["t", "i"], sort=False).dropna()
    spread = df["t"] - df["i"]
    recent = spread[spread.index >= spread.index.max() - pd.DateOffset(months=6)]
    return {
        "triggered": bool((recent < 0).any()),
        "value": f"10Y-3M now {spread.iloc[-1]:+.2f}%, 6m min {recent.min():+.2f}%",
    }


def sig_credit_stress() -> dict:
    """S9: Credit Stress 프록시 — 1 - pct(HYG/LQD, 10y) < 0.25 (스프레드 과열 압축)"""
    hyg = yahoo_closes("HYG", months=120)
    lqd = yahoo_closes("LQD", months=120)
    ratio = (pd.concat([hyg, lqd], axis=1, keys=["h", "l"], sort=False).dropna()
               .pipe(lambda d: d["h"] / d["l"]))
    ratio = ratio[ratio.index >= ratio.index.max() - pd.DateOffset(years=10)]
    pct = float((ratio < ratio.iloc[-1]).mean())
    stress = 1 - pct
    return {
        "triggered": bool(stress < 0.25),
        "value": f"HYG/LQD {ratio.iloc[-1]:.4f}, stress proxy {stress:.2f}",
    }


def sig_sloos() -> dict:
    """S10: SLOOS 긴축 (FRED 1회 시도 → manual_signals.json S10 폴백, 분기 갱신)"""
    try:
        s = fred_series("DRTSCILM", years=5)
        latest = float(s.iloc[-1])
        return {"triggered": bool(latest > 0),
                "value": f"Net tightening {latest:+.1f}% ({s.index[-1]:%Y-%m})"}
    except Exception:
        manual = {}
        if os.path.exists(MANUAL_PATH):
            with open(MANUAL_PATH, "r", encoding="utf-8") as f:
                manual = json.load(f)
        m = manual.get("S10", {})
        return {"triggered": bool(m.get("triggered", False)),
                "value": f"manual fallback (as of {m.get('as_of', '?')})"}


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


def eval_bull_bear():
    """BofA Bull & Bear Indicator 택티컬 오버레이 (manual_signals.json 'BB').
    핵심 10개 구조 스코어와 독립 운용해 Exhibit 11의 피크 평균(70%) 캘리브레이션을 보존한다."""
    manual = {}
    if os.path.exists(MANUAL_PATH):
        with open(MANUAL_PATH, "r", encoding="utf-8") as f:
            manual = json.load(f)
    bb = manual.get("BB")
    if not bb or bb.get("value") is None:
        return None
    val = float(bb["value"])
    prev = bb.get("prev")
    if val >= BB_SELL:
        status, emoji, label = "SELL", "🔴", "컨트래리안 매도 (극단적 낙관)"
    elif val <= BB_BUY:
        status, emoji, label = "BUY", "🟢", "컨트래리안 매수 (극단적 비관)"
    else:
        status, emoji, label = "NEUTRAL", "🟡", "중립 구간"
    delta = ""
    if isinstance(prev, (int, float)):
        arrow = "↑" if val > prev else ("↓" if val < prev else "→")
        delta = f" ({prev:.1f}{arrow}{val:.1f})"
    return {"value": val, "prev": prev, "status": status, "emoji": emoji,
            "label": label, "as_of": bb.get("as_of", "?"),
            "note": bb.get("note", ""), "delta": delta}


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
def update_history(date_str: str, pct: int, results: list, bb: dict = None) -> list:
    hist = []
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            hist = json.load(f)
    hist = [h for h in hist if h["date"] != date_str]
    entry = {
        "date": date_str,
        "pct_triggered": pct,
        "signals": {r["id"]: r["triggered"] for r in results},
        "details": {r["id"]: r["value"] for r in results},
    }
    if bb:
        entry["bull_bear"] = {"value": bb["value"], "status": bb["status"],
                              "as_of": bb["as_of"]}
    hist.append(entry)
    hist.sort(key=lambda h: h["date"])
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)
    # 오케스트레이터 신선도 체크용 latest.json (generated_at 필수)
    latest = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              **hist[-1]}
    with open(os.path.join(DATA_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1)
    return hist


def build_message(date_str: str, results: list, pct: int, bb: dict = None) -> str:
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
    if bb:
        struct_red = pct >= PEAK_AVG
        bb_red = bb["status"] == "SELL"
        if struct_red and bb_red:
            dual = "🔴🔴 <b>이중 확인</b> — 구조+센티먼트 동시 경고"
        elif struct_red or bb_red:
            dual = "🟠 <b>단일 경고</b> — 한 축만 점등"
        else:
            dual = "🟢 <b>이중 안정</b>"
        lines += [
            "🎯 <b>택티컬 오버레이</b> (Bull &amp; Bear)",
            f"{bb['emoji']} <b>{bb['value']:.1f}</b>/10 {bb['status']}{bb['delta']}",
            f"    <i>{_html.escape(bb['label'])} · as of {bb['as_of']}</i>",
            dual,
            "",
        ]
    cat_prev = None
    for r in results:
        if r["category"] != cat_prev:
            lines.append(f"<b>[{r['category']}]</b>")
            cat_prev = r["category"]
        mark = "🔺" if r["triggered"] else ("⚠️" if r["error"] else "▫️")
        tag = "" if r["mode"] == "auto" else " ✍️"
        lines.append(f"{mark} {_html.escape(r['name'])}{tag}")
        if r["value"] and not r["error"]:
            lines.append(f"    <i>{_html.escape(r['value'])}</i>")
        elif r["error"]:
            lines.append(f"    <i>{_html.escape(r['value'][:80])}</i>")
    lines += [
        "",
        f"기준: BofA Exhibit 11 (피크 평균 ~{PEAK_AVG}%) · B&amp;B는 독립 센티먼트 오버레이",
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
    bb = eval_bull_bear()
    update_history(today, pct, results, bb)
    msg = build_message(today, results, pct, bb)
    print(msg.replace("<b>", "").replace("</b>", "")
             .replace("<i>", "").replace("</i>", ""))
    if cfg["telegram_token"] and cfg["telegram_chat_id"]:
        send_telegram(msg, cfg["telegram_token"], cfg["telegram_chat_id"])
        print("\n[OK] Telegram sent")
    else:
        print("\n[SKIP] Telegram credentials not set")


if __name__ == "__main__":
    main()
