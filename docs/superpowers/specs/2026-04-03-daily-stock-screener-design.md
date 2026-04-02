# Daily Stock Screener Design

**Date:** 2026-04-03  
**Status:** Approved

## Overview

Add a daily stock screener that scans all Shanghai A-share stocks (~2000), scores them on technical + fundamental indicators, and pushes the top 10 highest-scoring stocks to Telegram each day after market close.

## Requirements

- **Universe:** All Shanghai A-share stocks (~2000), sourced from AkShare
- **Scoring:** Technical score (60%) + Fundamental score (40%) = Composite score (0–100)
- **Scheduling:** Triggered daily at 15:30 (after market close), runs until complete
- **Rate limiting:** Random 0.5–2s delay between stock requests, 3–5s pause between batches of 50
- **Output:** Push top 10 to Telegram with code, name, composite score, and brief reason
- **Storage:** Results persisted to new `screener_results` SQLite table for audit/history

## Architecture

```
Scheduler (15:30 daily)
    └── StockScreener.run_daily_scan()
            ├── get_sh_stock_universe() → ~2000 codes (AkShare)
            ├── Batch loop (50 stocks/batch, random delays)
            │       ├── fetch daily data (30 days, via existing fetcher_manager)
            │       ├── fetch fundamental snapshot (via existing FundamentalAdapter)
            │       ├── ScreenerScorer.score(tech_data, fund_data) → (tech, fund, total)
            │       └── save to screener_results table
            └── query top 10 → format → TelegramSender.send_to_telegram()
```

## Scoring System

### Technical Score (0–100)

| Component | Weight | Logic |
|-----------|--------|-------|
| Signal score (existing `signal_score`) | 40% | Normalize 0–100 directly |
| MA alignment | 15% | MA5>MA10>MA20 = 100, partial = 50, bearish = 0 |
| Volume status | 15% | 放量突破=100, 缩量回调=70, 量能不足=30, 其他=50 |
| Relative strength (rs_signal) | 30% | 强势=100, 中性=50, 弱势=0 |

### Fundamental Score (0–100)

| Component | Weight | Logic |
|-----------|--------|-------|
| ROE | 30% | ≥20%=100, 15–20%=80, 10–15%=60, 5–10%=40, <5%=10 |
| Gross margin | 25% | ≥50%=100, 30–50%=75, 20–30%=55, <20%=30 |
| Revenue YoY growth | 15% | ≥30%=100, 15–30%=80, 5–15%=60, 0–5%=40, <0=10 |
| Net profit YoY growth | 15% | Same scale as revenue |
| Debt-to-assets ratio | 15% | <40%=100, 40–60%=75, 60–70%=50, >70%=10 |

Missing financial data: component scores default to 50 (neutral).

### Composite Score

```
total = tech_score * 0.6 + fund_score * 0.4
```

## Components

### `src/services/stock_screener.py` (new)

`StockScreener` class:
- `get_sh_stock_universe() -> List[str]` — calls AkShare `stock_info_sh_name_code()`, returns list of 6-digit codes
- `run_daily_scan() -> List[ScreenerResult]` — main entry point; batches, rate limits, scores, saves, notifies
- `_scan_batch(codes: List[str]) -> List[ScreenerResult]` — processes one batch with per-stock retry

Rate limiting inside `_scan_batch`:
```python
import random, time
time.sleep(random.uniform(0.5, 2.0))  # between each stock
# between batches:
time.sleep(random.uniform(3.0, 5.0))
```

Retry: up to 2 retries per stock with 5s backoff on exception. Failed stocks are skipped (logged).

### `src/services/screener_scorer.py` (new)

`ScreenerScorer` class with static methods:
- `score_technical(trend_result: TrendAnalysisResult) -> float`
- `score_fundamental(financial_report: dict, growth: dict) -> float`
- `score(trend_result, financial_report, growth) -> ScreenerResult`

Returns `ScreenerResult(stock_code, stock_name, tech_score, fund_score, total_score, reasons)`.

### `src/storage.py` (modify)

Add `screener_results` table:
```sql
CREATE TABLE IF NOT EXISTS screener_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_date DATE NOT NULL,
    stock_code VARCHAR(16) NOT NULL,
    stock_name VARCHAR(64),
    tech_score REAL,
    fund_score REAL,
    total_score REAL,
    rank INTEGER,
    reasons TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(scan_date, stock_code)
)
```

New methods: `save_screener_results(results, date)`, `get_top_screener_results(date, limit=10)`.

### `main.py` (modify)

Add scheduled task: `schedule.every().day.at("15:30").do(run_screener_task)` in the `--schedule` block.
The task calls `StockScreener(config, fetcher_manager, db, telegram_sender).run_daily_scan()`.

### Telegram Notification Format

```
📊 每日选股 Top 10（2026-04-03）
扫描完成 1923/2000 只（耗时 2h14m）

🥇 1. 600519 贵州茅台 | 综合 87分
   技术 91（强势多头，放量突破）| 财务 80（ROE 28%，营收+22%）

🥈 2. 688XXX 某某科技 | 综合 84分
   技术 88（强势，均线多头）| 财务 77（毛利率高，低负债）
...
```

## Data Caching

- Daily price data: reuses existing `stock_daily` SQLite table — if today's data already cached, skip fetch
- Fundamental data: reuses existing `fundamental_snapshot` table — if snapshot exists and is <7 days old, skip fetch
- Universe list: cached in memory for the scan run; refreshed each day

## Error Handling

- `get_sh_stock_universe()` failure → abort scan, log error, send Telegram warning
- Per-stock fetch failure → skip after 2 retries, continue with next stock
- Score calculation error → skip stock, continue
- Final notification failure → log error (scan results already saved to DB)

## Out of Scope

- HK/US stocks (SH A-shares only per requirement)
- Web UI for ranking table (future)
- Auto-adding top 10 to stock pool (user chose Telegram-only output)
- LLM deep analysis of screened stocks (user chose score + brief reason only)
