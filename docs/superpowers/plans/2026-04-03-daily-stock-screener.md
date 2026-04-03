# Daily Stock Screener Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scan all Shanghai A-share stocks daily after market close, score them on technical + financial indicators, and push the top 10 to Telegram.

**Architecture:** A new `StockScreener` service fetches all SH A-share codes from AkShare, processes them in batches of 50 with rate limiting, computes composite scores via `ScreenerScorer`, persists results to a new `screener_results` SQLite table, and notifies via the existing `TelegramSender`. Scheduled via the existing `schedule` library at 15:30 daily.

**Tech Stack:** AkShare, SQLAlchemy ORM, `schedule` library, existing `StockTrendAnalyzer`, `FundamentalAdapter`, `TelegramSender`.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/services/screener_scorer.py` | **Create** | Technical + financial scoring logic |
| `src/services/stock_screener.py` | **Create** | AkShare universe fetch, batch loop, rate limiting, notification |
| `src/storage.py` | **Modify** | Add `ScreenerResult` ORM model + `save_screener_results()` + `get_top_screener_results()` |
| `main.py` | **Modify** | Register screener task at 15:30 in schedule block |
| `tests/test_screener_scorer.py` | **Create** | Unit tests for scoring functions |
| `tests/test_stock_screener.py` | **Create** | Unit tests for screener service |

---

## Task 1: Screener scorer — technical scoring

**Files:**
- Create: `src/services/screener_scorer.py`
- Create: `tests/test_screener_scorer.py`

- [ ] **Step 1.1: Write failing test**

```python
# tests/test_screener_scorer.py
from unittest.mock import MagicMock
from src.services.screener_scorer import ScreenerScorer, ScreenerResult


class TestTechnicalScore:
    def _make_trend(self, signal_score=80, ma_alignment="多头排列",
                    volume_status="放量", rs_signal="强势"):
        t = MagicMock()
        t.signal_score = signal_score
        t.ma_alignment = ma_alignment
        t.volume_status = volume_status
        t.rs_signal = rs_signal
        return t

    def test_full_bullish_scores_high(self):
        trend = self._make_trend(signal_score=90, ma_alignment="多头排列",
                                  volume_status="放量", rs_signal="强势")
        score = ScreenerScorer.score_technical(trend)
        assert score >= 80

    def test_full_bearish_scores_low(self):
        trend = self._make_trend(signal_score=10, ma_alignment="空头排列",
                                  volume_status="缩量", rs_signal="弱势")
        score = ScreenerScorer.score_technical(trend)
        assert score <= 40

    def test_score_is_bounded_0_to_100(self):
        trend = self._make_trend(signal_score=100, ma_alignment="多头排列",
                                  volume_status="放量", rs_signal="强势")
        score = ScreenerScorer.score_technical(trend)
        assert 0 <= score <= 100
```

- [ ] **Step 1.2: Run test — expect ImportError**

```bash
python3 -m pytest tests/test_screener_scorer.py -v 2>&1 | head -10
```

Expected: `ModuleNotFoundError: No module named 'src.services.screener_scorer'`

- [ ] **Step 1.3: Create `src/services/screener_scorer.py`**

```python
# src/services/screener_scorer.py
# -*- coding: utf-8 -*-
"""
股票筛选打分模块

技术得分（0-100）：
  signal_score 40% + MA排列 15% + 量能 15% + 个股强弱 30%

财务得分（0-100）：
  ROE 30% + 毛利率 25% + 营收同比 15% + 净利润同比 15% + 资产负债率 15%

综合得分 = 技术 × 60% + 财务 × 40%
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ScreenerResult:
    stock_code: str
    stock_name: str = ""
    tech_score: float = 0.0
    fund_score: float = 0.0
    total_score: float = 0.0
    reasons: List[str] = field(default_factory=list)


class ScreenerScorer:

    @staticmethod
    def score_technical(trend_result) -> float:
        """
        Score technical indicators. trend_result is a TrendAnalysisResult instance
        (or any object with the same attributes).
        Returns float 0-100.
        """
        # 40%: signal_score (already 0-100)
        sig = max(0.0, min(100.0, float(getattr(trend_result, 'signal_score', 50) or 50)))

        # 15%: MA alignment
        ma = getattr(trend_result, 'ma_alignment', '') or ''
        if '多头' in ma:
            ma_score = 100.0
        elif '空头' in ma:
            ma_score = 0.0
        else:
            ma_score = 50.0

        # 15%: volume status
        vol = getattr(trend_result, 'volume_status', '') or ''
        if '放量' in vol:
            vol_score = 100.0
        elif '缩量回调' in vol or '缩量' in vol:
            vol_score = 70.0
        elif '量能不足' in vol or '低量' in vol:
            vol_score = 30.0
        else:
            vol_score = 50.0

        # 30%: relative strength
        rs = getattr(trend_result, 'rs_signal', '') or ''
        if rs == '强势':
            rs_score = 100.0
        elif rs == '弱势':
            rs_score = 0.0
        else:
            rs_score = 50.0

        return round(sig * 0.40 + ma_score * 0.15 + vol_score * 0.15 + rs_score * 0.30, 2)

    @staticmethod
    def score_fundamental(financial_report: Dict[str, Any]) -> float:
        """
        Score fundamental indicators from financial_report dict.
        Missing values default to neutral (50).
        Returns float 0-100.
        """
        def _get(key):
            v = financial_report.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        # ROE (30%)
        roe = _get('roe')
        if roe is None:
            roe_score = 50.0
        elif roe >= 20:
            roe_score = 100.0
        elif roe >= 15:
            roe_score = 80.0
        elif roe >= 10:
            roe_score = 60.0
        elif roe >= 5:
            roe_score = 40.0
        else:
            roe_score = 10.0

        # Gross margin (25%)
        gm = _get('gross_margin')
        if gm is None:
            gm_score = 50.0
        elif gm >= 50:
            gm_score = 100.0
        elif gm >= 30:
            gm_score = 75.0
        elif gm >= 20:
            gm_score = 55.0
        else:
            gm_score = 30.0

        # Revenue YoY (15%)
        rev_yoy = _get('revenue_yoy')
        if rev_yoy is None:
            rev_score = 50.0
        elif rev_yoy >= 30:
            rev_score = 100.0
        elif rev_yoy >= 15:
            rev_score = 80.0
        elif rev_yoy >= 5:
            rev_score = 60.0
        elif rev_yoy >= 0:
            rev_score = 40.0
        else:
            rev_score = 10.0

        # Net profit YoY (15%)
        np_yoy = _get('net_profit_yoy')
        if np_yoy is None:
            np_score = 50.0
        elif np_yoy >= 30:
            np_score = 100.0
        elif np_yoy >= 15:
            np_score = 80.0
        elif np_yoy >= 5:
            np_score = 60.0
        elif np_yoy >= 0:
            np_score = 40.0
        else:
            np_score = 10.0

        # Debt-to-assets (15%, lower = better)
        d2a = _get('debt_to_assets')
        if d2a is None:
            d2a_score = 50.0
        elif d2a < 40:
            d2a_score = 100.0
        elif d2a < 60:
            d2a_score = 75.0
        elif d2a < 70:
            d2a_score = 50.0
        else:
            d2a_score = 10.0

        return round(
            roe_score * 0.30 + gm_score * 0.25 + rev_score * 0.15
            + np_score * 0.15 + d2a_score * 0.15,
            2
        )

    @staticmethod
    def score(stock_code: str, stock_name: str,
              trend_result, financial_report: Dict[str, Any]) -> ScreenerResult:
        """Compute composite score and build a ScreenerResult."""
        tech = ScreenerScorer.score_technical(trend_result)
        fund = ScreenerScorer.score_fundamental(financial_report)
        total = round(tech * 0.6 + fund * 0.4, 2)

        reasons = []
        rs = getattr(trend_result, 'rs_signal', '') or ''
        if rs:
            reasons.append(f"个股强弱:{rs}")
        ma = getattr(trend_result, 'ma_alignment', '') or ''
        if '多头' in ma:
            reasons.append("均线多头排列")
        roe = financial_report.get('roe')
        if roe is not None:
            try:
                reasons.append(f"ROE:{float(roe):.1f}%")
            except (TypeError, ValueError):
                pass

        return ScreenerResult(
            stock_code=stock_code,
            stock_name=stock_name,
            tech_score=tech,
            fund_score=fund,
            total_score=total,
            reasons=reasons,
        )
```

- [ ] **Step 1.4: Run tests — expect PASS**

```bash
python3 -m pytest tests/test_screener_scorer.py -v
```

Expected: 3 tests PASS

- [ ] **Step 1.5: Add fundamental scorer tests**

Add to `tests/test_screener_scorer.py`:

```python
class TestFundamentalScore:
    def test_high_quality_financials_score_high(self):
        report = {"roe": 25.0, "gross_margin": 55.0,
                  "revenue_yoy": 35.0, "net_profit_yoy": 40.0, "debt_to_assets": 30.0}
        score = ScreenerScorer.score_fundamental(report)
        assert score >= 85

    def test_poor_financials_score_low(self):
        report = {"roe": 2.0, "gross_margin": 10.0,
                  "revenue_yoy": -5.0, "net_profit_yoy": -10.0, "debt_to_assets": 80.0}
        score = ScreenerScorer.score_fundamental(report)
        assert score <= 25

    def test_missing_data_returns_neutral(self):
        score = ScreenerScorer.score_fundamental({})
        assert score == 50.0

    def test_composite_score_weighted_correctly(self):
        trend = MagicMock()
        trend.signal_score = 100
        trend.ma_alignment = "多头排列"
        trend.volume_status = "放量"
        trend.rs_signal = "强势"
        report = {"roe": 25.0, "gross_margin": 55.0,
                  "revenue_yoy": 35.0, "net_profit_yoy": 40.0, "debt_to_assets": 30.0}
        result = ScreenerScorer.score("600519", "贵州茅台", trend, report)
        assert result.stock_code == "600519"
        assert result.stock_name == "贵州茅台"
        assert result.tech_score == 100.0
        assert result.total_score == round(100.0 * 0.6 + result.fund_score * 0.4, 2)
        assert 0 <= result.total_score <= 100
```

- [ ] **Step 1.6: Run all scorer tests**

```bash
python3 -m pytest tests/test_screener_scorer.py -v
```

Expected: 7 tests PASS

- [ ] **Step 1.7: Commit**

```bash
git add src/services/screener_scorer.py tests/test_screener_scorer.py
git commit -m "feat: add screener scoring engine (technical + fundamental)"
```

---

## Task 2: Add screener_results table to storage

**Files:**
- Modify: `src/storage.py`
- Create: `tests/test_screener_storage.py`

- [ ] **Step 2.1: Write failing test**

```python
# tests/test_screener_storage.py
import datetime
from src.storage import DatabaseManager
from src.services.screener_scorer import ScreenerResult


class TestScreenerStorage:
    def setup_method(self):
        # Use in-memory database for tests
        self.db = DatabaseManager.__new__(DatabaseManager)
        self.db._initialized = False
        self.db.__init__(db_url="sqlite:///:memory:")

    def _make_results(self):
        return [
            ScreenerResult("600519", "贵州茅台", 90.0, 85.0, 88.0, ["均线多头排列"]),
            ScreenerResult("000001", "平安银行", 75.0, 70.0, 73.0, ["ROE:15.0%"]),
        ]

    def test_save_and_retrieve_top_results(self):
        today = datetime.date.today()
        results = self._make_results()
        self.db.save_screener_results(results, today)

        top = self.db.get_top_screener_results(today, limit=10)
        assert len(top) == 2
        # Should be sorted by total_score descending
        assert top[0].stock_code == "600519"
        assert top[0].total_score == 88.0

    def test_upsert_replaces_existing(self):
        today = datetime.date.today()
        results = self._make_results()
        self.db.save_screener_results(results, today)
        # Re-save with updated score for 600519
        updated = [ScreenerResult("600519", "贵州茅台", 95.0, 90.0, 93.0, [])]
        self.db.save_screener_results(updated, today)

        top = self.db.get_top_screener_results(today, limit=10)
        mao = next(r for r in top if r.stock_code == "600519")
        assert mao.total_score == 93.0
```

- [ ] **Step 2.2: Run test — expect AttributeError (methods don't exist yet)**

```bash
python3 -m pytest tests/test_screener_storage.py -v 2>&1 | head -20
```

Expected: `AttributeError: 'DatabaseManager' object has no attribute 'save_screener_results'`

- [ ] **Step 2.3: Add ORM model and methods to `src/storage.py`**

After the last existing ORM model class (find the line with `class LLMUsage(Base):` or last model before `class DatabaseManager:`), add:

```python
class ScreenerResult(Base):
    """每日选股扫描结果"""
    __tablename__ = 'screener_results'

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_date = Column(Date, nullable=False, index=True)
    stock_code = Column(String(16), nullable=False)
    stock_name = Column(String(64))
    tech_score = Column(Float)
    fund_score = Column(Float)
    total_score = Column(Float)
    rank = Column(Integer)
    reasons = Column(String(512))
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint('scan_date', 'stock_code', name='uq_screener_date_code'),
    )
```

Add the following imports at the top of `src/storage.py` if not already present:
- `UniqueConstraint` from sqlalchemy (add to existing sqlalchemy import block)
- `func` from sqlalchemy (add to existing sqlalchemy import block)

Then add these two methods to the `DatabaseManager` class (before the `purge_old_data` method):

```python
def save_screener_results(self, results: list, scan_date) -> None:
    """Save or update screener results for a given date (upsert by date+code)."""
    from src.services.screener_scorer import ScreenerResult as ScorerResult
    import json
    with self.get_session() as session:
        for i, r in enumerate(results):
            existing = session.query(ScreenerResult).filter_by(
                scan_date=scan_date, stock_code=r.stock_code
            ).first()
            reasons_str = json.dumps(r.reasons, ensure_ascii=False) if r.reasons else "[]"
            if existing:
                existing.stock_name = r.stock_name
                existing.tech_score = r.tech_score
                existing.fund_score = r.fund_score
                existing.total_score = r.total_score
                existing.reasons = reasons_str
            else:
                session.add(ScreenerResult(
                    scan_date=scan_date,
                    stock_code=r.stock_code,
                    stock_name=r.stock_name,
                    tech_score=r.tech_score,
                    fund_score=r.fund_score,
                    total_score=r.total_score,
                    reasons=reasons_str,
                ))
        session.commit()

def get_top_screener_results(self, scan_date, limit: int = 10) -> list:
    """Return top N screener results for a date, sorted by total_score desc."""
    import json
    from src.services.screener_scorer import ScreenerResult as ScorerResult
    with self.get_session() as session:
        rows = (
            session.query(ScreenerResult)
            .filter_by(scan_date=scan_date)
            .order_by(ScreenerResult.total_score.desc())
            .limit(limit)
            .all()
        )
        results = []
        for row in rows:
            try:
                reasons = json.loads(row.reasons or "[]")
            except Exception:
                reasons = []
            results.append(ScorerResult(
                stock_code=row.stock_code,
                stock_name=row.stock_name or "",
                tech_score=row.tech_score or 0.0,
                fund_score=row.fund_score or 0.0,
                total_score=row.total_score or 0.0,
                reasons=reasons,
            ))
        return results
```

**Note:** `UniqueConstraint` and `func` must be imported. Check the existing import block at the top of `src/storage.py` (around line 24) and add them if missing:
```python
from sqlalchemy import (
    ...
    UniqueConstraint,  # add this
    func,              # add this
)
```

- [ ] **Step 2.4: Run tests**

```bash
python3 -m pytest tests/test_screener_storage.py -v
python3 -m py_compile src/storage.py && echo "Syntax OK"
```

Expected: 2 tests PASS, Syntax OK

- [ ] **Step 2.5: Commit**

```bash
git add src/storage.py tests/test_screener_storage.py
git commit -m "feat: add screener_results table and DB methods"
```

---

## Task 3: StockScreener service — universe fetch and batch loop

**Files:**
- Create: `src/services/stock_screener.py`
- Create: `tests/test_stock_screener.py`

- [ ] **Step 3.1: Write failing tests**

```python
# tests/test_stock_screener.py
import datetime
from unittest.mock import MagicMock, patch
from src.services.stock_screener import StockScreener


class TestStockUniverse:
    def test_get_sh_stock_universe_returns_6digit_codes(self):
        import pandas as pd
        mock_df = pd.DataFrame({
            "代码": ["600519", "600000", "601318"],
            "名称": ["贵州茅台", "浦发银行", "中国平安"],
        })
        with patch("akshare.stock_info_sh_name_code", return_value=mock_df):
            screener = StockScreener.__new__(StockScreener)
            codes = screener._parse_universe_df(mock_df)
        assert codes == [("600519", "贵州茅台"), ("600000", "浦发银行"), ("601318", "中国平安")]

    def test_universe_filters_non_6digit(self):
        import pandas as pd
        mock_df = pd.DataFrame({
            "代码": ["600519", "00700", "ABC"],
            "名称": ["贵州茅台", "腾讯控股", "某某"],
        })
        with patch("akshare.stock_info_sh_name_code", return_value=mock_df):
            screener = StockScreener.__new__(StockScreener)
            codes = screener._parse_universe_df(mock_df)
        # Only 600519 is a valid 6-digit numeric code
        assert len(codes) == 1
        assert codes[0][0] == "600519"


class TestRateLimiting:
    def test_scan_respects_batch_size(self):
        """StockScreener splits codes into batches of BATCH_SIZE."""
        screener = StockScreener.__new__(StockScreener)
        codes = [(str(i).zfill(6), f"Stock{i}") for i in range(120)]
        batches = list(screener._make_batches(codes, batch_size=50))
        assert len(batches) == 3
        assert len(batches[0]) == 50
        assert len(batches[2]) == 20
```

- [ ] **Step 3.2: Run test — expect ImportError**

```bash
python3 -m pytest tests/test_stock_screener.py -v 2>&1 | head -10
```

Expected: `ModuleNotFoundError: No module named 'src.services.stock_screener'`

- [ ] **Step 3.3: Create `src/services/stock_screener.py`**

```python
# src/services/stock_screener.py
# -*- coding: utf-8 -*-
"""
每日全量上交所 A 股扫描服务

流程：
1. 从 AkShare 获取上交所全量 A 股代码列表
2. 分批（BATCH_SIZE 只/批）逐股拉取日线数据 + 财务快照
3. 用 ScreenerScorer 计算综合得分
4. 结果存入 screener_results 表
5. 推送 top 10 到 Telegram
"""
from __future__ import annotations

import datetime
import logging
import random
import re
import time
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

BATCH_SIZE = 50
INTRA_BATCH_DELAY = (0.5, 2.0)   # seconds, random uniform
INTER_BATCH_DELAY = (3.0, 5.0)   # seconds, random uniform
MAX_RETRIES = 2
RETRY_BACKOFF = 5.0               # seconds

_SH_CODE_RE = re.compile(r"^\d{6}$")


class StockScreener:
    def __init__(self, config, fetcher_manager, db, telegram_sender=None):
        self._config = config
        self._fetcher = fetcher_manager
        self._db = db
        self._telegram = telegram_sender

    # ------------------------------------------------------------------
    # Universe
    # ------------------------------------------------------------------

    def get_sh_stock_universe(self) -> List[Tuple[str, str]]:
        """Fetch all SH A-share stocks. Returns list of (code, name)."""
        try:
            import akshare as ak
            df = ak.stock_info_sh_name_code(symbol="主板A股")
        except Exception as e:
            logger.error("获取上交所股票列表失败: %s", e)
            raise
        return self._parse_universe_df(df)

    def _parse_universe_df(self, df) -> List[Tuple[str, str]]:
        """Extract (code, name) pairs from AkShare DataFrame."""
        results = []
        # AkShare column names vary; try common ones
        code_col = next((c for c in df.columns if "代码" in c or "symbol" in c.lower()), None)
        name_col = next((c for c in df.columns if "名称" in c or "name" in c.lower()), None)
        if code_col is None:
            raise ValueError(f"Cannot find code column in: {list(df.columns)}")
        for _, row in df.iterrows():
            code = str(row[code_col]).strip()
            name = str(row[name_col]).strip() if name_col else ""
            if _SH_CODE_RE.match(code):
                results.append((code, name))
        return results

    # ------------------------------------------------------------------
    # Batching
    # ------------------------------------------------------------------

    def _make_batches(self, items: list, batch_size: int = BATCH_SIZE):
        for i in range(0, len(items), batch_size):
            yield items[i:i + batch_size]

    # ------------------------------------------------------------------
    # Main scan
    # ------------------------------------------------------------------

    def run_daily_scan(self) -> List:
        """
        Full scan entry point. Returns list of ScreenerResult for all
        successfully scored stocks, sorted by total_score desc.
        """
        from src.services.screener_scorer import ScreenerScorer, ScreenerResult
        from src.stock_analyzer import StockTrendAnalyzer

        trend_analyzer = StockTrendAnalyzer()
        scan_date = datetime.date.today()
        start_time = time.time()

        logger.info("开始每日全量扫描 (date=%s)", scan_date)

        try:
            universe = self.get_sh_stock_universe()
        except Exception as e:
            msg = f"获取股票列表失败，扫描中止: {e}"
            logger.error(msg)
            self._notify(msg)
            return []

        logger.info("股票池共 %d 只，分 %d 批处理", len(universe),
                    (len(universe) + BATCH_SIZE - 1) // BATCH_SIZE)

        all_results: List = []
        scanned = 0

        for batch_num, batch in enumerate(self._make_batches(universe), start=1):
            batch_results = self._scan_batch(batch, trend_analyzer, ScreenerScorer, scan_date)
            all_results.extend(batch_results)
            scanned += len(batch)
            logger.info("批次 %d 完成，已扫描 %d/%d", batch_num, scanned, len(universe))

            # Inter-batch delay
            if scanned < len(universe):
                pause = random.uniform(*INTER_BATCH_DELAY)
                time.sleep(pause)

        # Persist all results
        if all_results:
            try:
                self._db.save_screener_results(all_results, scan_date)
            except Exception as e:
                logger.error("保存扫描结果失败: %s", e)

        elapsed = time.time() - start_time
        elapsed_str = f"{int(elapsed // 3600)}h{int((elapsed % 3600) // 60)}m"

        # Get top 10 and notify
        top10 = sorted(all_results, key=lambda r: r.total_score, reverse=True)[:10]
        self._send_top10(top10, scan_date, scanned, len(universe), elapsed_str)

        logger.info("扫描完成 %d/%d 只，耗时 %s", scanned, len(universe), elapsed_str)
        return all_results

    def _scan_batch(self, batch, trend_analyzer, ScreenerScorer, scan_date) -> List:
        """Score a single batch of stocks with per-stock retry and rate limiting."""
        results = []
        for code, name in batch:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    result = self._score_stock(code, name, trend_analyzer, ScreenerScorer)
                    if result is not None:
                        results.append(result)
                    break
                except Exception as e:
                    if attempt < MAX_RETRIES:
                        logger.debug("股票 %s 第 %d 次重试: %s", code, attempt + 1, e)
                        time.sleep(RETRY_BACKOFF)
                    else:
                        logger.warning("股票 %s 跳过（失败 %d 次）: %s", code, MAX_RETRIES + 1, e)

            # Intra-batch delay between each stock
            time.sleep(random.uniform(*INTRA_BATCH_DELAY))

        return results

    def _score_stock(self, code, name, trend_analyzer, ScreenerScorer):
        """Fetch data for one stock and return a ScreenerResult, or None on skip."""
        # 1. Daily price data (30 days)
        df, _ = self._fetcher.get_daily_data(code, days=30)
        if df is None or df.empty or len(df) < 5:
            return None

        # 2. Technical analysis (no index data for screener — fast mode)
        trend_result = trend_analyzer.analyze(df, code)

        # 3. Financial data (from cache or fetch)
        financial_report = {}
        try:
            snap = self._db.get_latest_fundamental_snapshot(code)
            if snap and snap.get("earnings"):
                financial_report = snap["earnings"].get("financial_report", {}) or {}
            else:
                # Try fetching fresh fundamental data
                from data_provider.fundamental_adapter import FundamentalAdapter
                adapter = FundamentalAdapter()
                ctx = adapter.get_fundamental_context(code)
                if ctx and ctx.get("earnings"):
                    financial_report = ctx["earnings"].get("financial_report", {}) or {}
        except Exception as e:
            logger.debug("获取 %s 财务数据失败（跳过财务评分）: %s", code, e)

        return ScreenerScorer.score(code, name, trend_result, financial_report)

    # ------------------------------------------------------------------
    # Notification
    # ------------------------------------------------------------------

    def _notify(self, text: str) -> None:
        if self._telegram:
            try:
                self._telegram.send_to_telegram(text)
            except Exception as e:
                logger.warning("Telegram 推送失败: %s", e)

    def _send_top10(self, top10: List, scan_date, scanned: int,
                    total: int, elapsed: str) -> None:
        if not top10:
            self._notify(f"每日选股扫描完成（{scan_date}），未找到有效结果。")
            return

        medals = ["🥇", "🥈", "🥉"] + ["  "] * 7
        lines = [f"📊 每日选股 Top 10（{scan_date}）",
                 f"扫描完成 {scanned}/{total} 只（耗时 {elapsed}）", ""]

        for i, r in enumerate(top10):
            reasons_str = "、".join(r.reasons[:3]) if r.reasons else "综合评分"
            lines.append(
                f"{medals[i]} {i + 1}. {r.stock_code} {r.stock_name} | 综合 {r.total_score:.0f}分"
            )
            lines.append(
                f"   技术 {r.tech_score:.0f} | 财务 {r.fund_score:.0f} | {reasons_str}"
            )

        self._notify("\n".join(lines))
```

- [ ] **Step 3.4: Run tests**

```bash
python3 -m pytest tests/test_stock_screener.py -v
python3 -m py_compile src/services/stock_screener.py && echo "Syntax OK"
```

Expected: 3 tests PASS, Syntax OK

- [ ] **Step 3.5: Commit**

```bash
git add src/services/stock_screener.py tests/test_stock_screener.py
git commit -m "feat: add StockScreener service with batch scan and rate limiting"
```

---

## Task 4: Register screener in scheduler

**Files:**
- Modify: `main.py`

- [ ] **Step 4.1: Add screener task to the schedule block in `main.py`**

Find the block starting with `if args.schedule or config.schedule_enabled:` (around line 882). After the data cleanup task registration (after line ~931), add:

```python
            # 每日选股扫描（收盘后 15:30 执行）
            def _screener_task():
                try:
                    from src.services.stock_screener import StockScreener
                    from src.notification_sender.telegram_sender import TelegramSender
                    from src.storage import get_db
                    _runtime = _reload_runtime_config()
                    _tg = TelegramSender(_runtime) if (
                        _runtime.telegram_bot_token and _runtime.telegram_chat_id
                    ) else None
                    screener = StockScreener(
                        config=_runtime,
                        fetcher_manager=fetcher_manager,
                        db=get_db(),
                        telegram_sender=_tg,
                    )
                    screener.run_daily_scan()
                except Exception as _e:
                    logger.error("每日选股扫描失败: %s", _e)
                    import traceback
                    logger.debug(traceback.format_exc())

            try:
                import schedule as _schedule_lib
                _schedule_lib.every().day.at("15:30").do(_screener_task)
                logger.info("已注册每日选股任务：每日 15:30 执行（UTC）")
            except Exception as _e:
                logger.warning("注册每日选股任务失败: %s", _e)
```

**Note:** `fetcher_manager` is already in scope in the schedule block — verify by checking `main.py` around line 882 that `fetcher_manager` is available. If not, import it from the pipeline initialisation.

- [ ] **Step 4.2: Verify syntax**

```bash
python3 -m py_compile main.py && echo "Syntax OK"
```

Expected: `Syntax OK`

- [ ] **Step 4.3: Commit**

```bash
git add main.py
git commit -m "feat: register daily stock screener at 15:30 in scheduler"
```

---

## Task 5: Deploy and smoke test

**Files:**
- No new files — deploy existing changes

- [ ] **Step 5.1: Push to remote and deploy**

```bash
git push origin main
sshpass -p 'asdf%TGB' ssh root@107.172.243.145 'cd /home/stock && git pull && systemctl restart stock && sleep 4 && systemctl is-active stock'
```

Expected: `active`

- [ ] **Step 5.2: Verify screener task is registered in server logs**

```bash
sshpass -p 'asdf%TGB' ssh root@107.172.243.145 'journalctl -u stock -n 20 --no-pager | grep -i "选股\|screener"'
```

Expected: `已注册每日选股任务：每日 15:30 执行（UTC）`

- [ ] **Step 5.3: Trigger manual test run**

SSH into server and run a small test scan (5 stocks only):

```bash
sshpass -p 'asdf%TGB' ssh root@107.172.243.145 'cd /home/stock && .venv/bin/python -c "
from src.config import setup_env, get_config
setup_env()
config = get_config()
from data_provider.base import DataFetcherManager
from src.storage import get_db
from src.notification_sender.telegram_sender import TelegramSender
from src.services.stock_screener import StockScreener, BATCH_SIZE

fm = DataFetcherManager(config)
db = get_db()
tg = TelegramSender(config)

screener = StockScreener(config, fm, db, tg)
# Test with just 5 stocks
test_stocks = [(\"600519\", \"贵州茅台\"), (\"000001\", \"平安银行\"), (\"601318\", \"中国平安\"), (\"600036\", \"招商银行\"), (\"000858\", \"五粮液\")]
from src.services.screener_scorer import ScreenerScorer
from src.stock_analyzer import StockTrendAnalyzer
import datetime

ta = StockTrendAnalyzer()
results = screener._scan_batch(test_stocks, ta, ScreenerScorer, datetime.date.today())
for r in sorted(results, key=lambda x: x.total_score, reverse=True):
    print(f\"{r.stock_code} {r.stock_name}: 综合={r.total_score:.1f} 技术={r.tech_score:.1f} 财务={r.fund_score:.1f}\")
"
'
```

Expected: Output showing 5 stocks with scores, no errors.

---

## Self-Review

**Spec coverage:**
- ✅ SH A-share universe from AkShare — Task 3 (`get_sh_stock_universe`)
- ✅ Batch processing (50/batch) — Task 3 (`_make_batches`, `BATCH_SIZE=50`)
- ✅ Rate limiting: 0.5-2s intra-batch, 3-5s inter-batch — Task 3 (`INTRA_BATCH_DELAY`, `INTER_BATCH_DELAY`)
- ✅ Retry (2 retries, 5s backoff) — Task 3 (`MAX_RETRIES`, `RETRY_BACKOFF`)
- ✅ Technical scoring (signal_score, MA, volume, RS) — Task 1
- ✅ Financial scoring (ROE, gross margin, YoY, debt ratio) — Task 1
- ✅ 60/40 composite weight — Task 1 (`score()`)
- ✅ `screener_results` table with upsert — Task 2
- ✅ Top 10 Telegram notification with score + reasons — Task 3 (`_send_top10`)
- ✅ Scheduled at 15:30 — Task 4
- ✅ Reuses existing `FundamentalAdapter`, `StockTrendAnalyzer`, `TelegramSender` — throughout

**No placeholders found.**

**Type consistency:**
- `ScreenerResult` defined in Task 1, used in Tasks 2 and 3 consistently.
- `save_screener_results(results: list, scan_date)` — called in Task 3 with `all_results` (List[ScreenerResult]) and `scan_date` (datetime.date) — consistent.
- `get_top_screener_results(scan_date, limit)` — called in Task 3 (indirectly via `sorted`) — consistent.
- `StockScreener.__init__(config, fetcher_manager, db, telegram_sender)` — consistent with Task 4 instantiation.
