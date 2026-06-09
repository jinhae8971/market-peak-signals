# market-peak-signals

BofA Exhibit 11 방법론 기반 **시장 고점 시그널 10종 일일 모니터**.

- 매일 07:30 KST — 시그널 계산 → Telegram 발송 → `docs/data/history.json` 누적 커밋
- 대시보드: https://jinhae8971.github.io/market-peak-signals/
- 자동 7종: FRED · Yahoo · CNN F&G · multpl · BLS · Shiller(datahub) — 다중 폴백 + 실패 시 전일값 유지
- 수동 3종(BofA 독점): `manual_overrides.json` 에서 `triggered`/`as_of` 갱신
  - `sell_side` BofA Sell Side Indicator
  - `ltg` S&P500 LTG 5yr Z>1
  - `mna` M&A deals 10yr Z>1
- 경보 기준: 점등률 ≥70% (과거 7회 고점 평균) 🔴
