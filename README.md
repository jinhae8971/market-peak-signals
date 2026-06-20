# Market Peak Signals

BofA Exhibit 11("List of signals to watch for a market peak") 기반 시장 피크 시그널 일일 모니터.

- **Telegram**: 매일 07:30 KST 10개 시그널 점검 결과 발송
- **Dashboard**: https://jinhae8971.github.io/market-peak-signals/ (추세 차트 + 히트맵)

## 시그널 구성

| ID | 시그널 | 방식 | 소스 |
|---|---|---|---|
| S1 | CB 소비자신뢰지수 > 110 | 수동 | Conference Board (월간) |
| S2 | CB 주가상승 기대 Net% > 20 | 수동 | Conference Board (월간) |
| S3 | BofA Sell Side Indicator 'Sell' | 수동 | BofA (월간, 언론 보도) |
| S4 | S&P500 LTG 5y Z > 1 | 수동 | BofA/IBES |
| S5 | M&A 건수 10y Z > 1 | 수동 | BofA |
| S6 | trailing PE + YoY CPI 10y Z > 1 | 자동 | multpl PE + BLS CPI API |
| S7 | Low PE 6m -2.5ppt 언더퍼폼 | 자동 | IVE vs IVW (프록시) |
| S8 | 금리역전 (최근 6m) | 자동 | Yahoo ^TNX − ^IRX |
| S9 | Credit Stress < 0.25 | 자동 | HYG/LQD 10y 백분위 (프록시) |
| S10 | SLOOS 대출태도 긴축 | 자동 | FRED DRTSCILM (차단 시 manual S10 폴백) |

## 수동 시그널 갱신

`manual_signals.json`의 `triggered` / `as_of` / `note`를 월 1회 갱신 후 push.
다음 실행부터 자동 반영됩니다.

## 판정 기준

과거 7번의 시장 피크 평균 트리거율 ≈ 70%.
- 🔴 ≥70% 피크 경보 · 🟠 ≥50% 주의 · 🟢 <50% 안정

## 택티컬 오버레이 — BofA Bull & Bear Indicator

핵심 10개 구조 시그널과 **독립적으로** 운용되는 센티먼트 오버레이. 분모(10)에 포함하지 않아 Exhibit 11 피크 평균(70%) 캘리브레이션을 보존한다.

- `manual_signals.json`의 `BB.value`(0~10) / `prev` / `as_of`를 BofA 발표치 기준 월 1회 갱신
- 판정: **≥8.0 🔴 SELL**(극단적 낙관, 컨트래리안 매도) · **≤2.0 🟢 BUY**(극단적 비관, 컨트래리안 매수) · 그 외 🟡 NEUTRAL
- **이중 확인**: 구조 스코어 ≥70% & B&B SELL 동시 점등 시 🔴🔴 최우선 경계 (Telegram·대시보드 동시 표시)
- 대시보드 트렌드 차트에 우측 보조축(0~10)으로 오버레이되어 구조 vs 센티먼트를 한눈에 비교
