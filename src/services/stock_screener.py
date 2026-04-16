# src/services/stock_screener.py
# -*- coding: utf-8 -*-
"""
每日全量 A 股扫描服务（沪深两市）

流程：
1. 优先从 market_daily_cache 获取全量 A 股代码列表（SH+SZ），降级使用 AkShare 上交所主板列表
2. 分批（BATCH_SIZE 只/批）逐股从缓存读取日线数据 + 财务快照，无缓存时降级实时拉取
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
from typing import List, Tuple

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

    def get_universe(self) -> List[Tuple[str, str]]:
        """Get full A-share universe from market_daily_cache. Falls back to AkShare SH list."""
        universe = self._db.get_cached_universe()
        if universe:
            logger.info("从缓存加载股票池：%d 只", len(universe))
            return universe
        # Fallback: SH main board only (legacy)
        logger.warning("缓存为空，降级使用上交所主板股票列表")
        return self.get_sh_stock_universe()

    def get_sh_stock_universe(self) -> List[Tuple[str, str]]:
        """Legacy: fetch SH A-share list from AkShare. Used as fallback."""
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
        from src.services.screener_scorer import ScreenerScorer
        from src.stock_analyzer import StockTrendAnalyzer

        trend_analyzer = StockTrendAnalyzer()
        scan_date = datetime.date.today()
        start_time = time.time()

        logger.info("开始每日全量扫描 (date=%s)", scan_date)

        try:
            universe = self.get_universe()
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

            # Inter-batch delay removed: cache reads do not need rate limiting

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

            # Intra-batch delay removed: cache reads do not need rate limiting

        return results

    def _score_stock(self, code, name, trend_analyzer, ScreenerScorer):
        """Fetch data from cache (preferred) or live, then score."""
        # 1. Try cache first
        df = self._db.get_market_cache_for_stock(code, days=30)

        if df is None or df.empty or len(df) < 5:
            # 2. Fallback to live fetch
            if self._fetcher is not None:
                df, _ = self._fetcher.get_daily_data(code, days=30)
            if df is None or df.empty or len(df) < 5:
                return None

        # 3. Rename columns if needed — cache uses: open, high, low, close, volume, amount
        # TrendAnalyzer expects: standard OHLCV DataFrame with date index or date column
        if 'trade_date' in df.columns and 'date' not in df.columns:
            df = df.rename(columns={'trade_date': 'date'})

        # 4. Technical analysis
        trend_result = trend_analyzer.analyze(df, code)

        # 5. Financial data from DB cache or FundamentalAdapter
        financial_report = {}
        try:
            snap = self._db.get_latest_fundamental_snapshot(code)
            if snap and snap.get("earnings"):
                financial_report = snap["earnings"].get("financial_report", {}) or {}
            else:
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
        lines = [f"📊 全市场选股 Top 10（{scan_date}）",
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
