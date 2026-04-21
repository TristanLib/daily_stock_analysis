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
    # Market index helper
    # ------------------------------------------------------------------

    def _fetch_market_index(self):
        """
        拉取上证综指日线数据，用于全市场扫描的个股强弱（RS）计算。
        使用新浪数据源，境外服务器可访问。

        Returns:
            pd.DataFrame with columns [date, close], or None on failure.
        """
        try:
            import akshare as ak
            import pandas as pd
            from datetime import date, timedelta

            df = ak.stock_zh_index_daily(symbol="sh000001")
            if df is None or df.empty:
                return None

            # 只保留 date + close，截取近 60 个日历日（约 40 交易日）
            # 统一转为字符串再比较，避免 datetime.date vs str 类型错误
            cutoff = str(date.today() - timedelta(days=60))
            df = df[df['date'].astype(str) >= cutoff][['date', 'close']].copy()
            df = df.reset_index(drop=True)
            logger.info("上证指数数据获取成功，共 %d 条，RS 计算已启用", len(df))
            return df
        except Exception as e:
            logger.warning("上证指数数据获取失败，RS 评分将使用中性值: %s", e)
            return None

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

        # 先追踪上次 Top10 的次日表现，再开始本次扫描
        self._run_tracking_report(scan_date)

        try:
            universe = self.get_universe()
        except Exception as e:
            msg = f"获取股票列表失败，扫描中止: {e}"
            logger.error(msg)
            self._notify(msg)
            return []

        logger.info("股票池共 %d 只，分 %d 批处理", len(universe),
                    (len(universe) + BATCH_SIZE - 1) // BATCH_SIZE)

        # 一次性拉取大盘指数数据，供所有股票 RS 计算共用
        df_index = self._fetch_market_index()

        all_results: List = []
        scanned = 0

        for batch_num, batch in enumerate(self._make_batches(universe), start=1):
            batch_results = self._scan_batch(
                batch, trend_analyzer, ScreenerScorer, scan_date,
                cache_only=True, df_index=df_index,
            )
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

        # Save tracking records for this scan's top 10 (next scan will fill prices)
        try:
            self._db.save_screener_tracking(top10, scan_date)
            logger.info("已保存 Top10 追踪记录（%s）", scan_date)
        except Exception as e:
            logger.warning("保存追踪记录失败: %s", e)

        logger.info("扫描完成 %d/%d 只，耗时 %s", scanned, len(universe), elapsed_str)
        return all_results

    def _scan_batch(self, batch, trend_analyzer, ScreenerScorer, scan_date,
                    cache_only: bool = False, df_index=None) -> List:
        """Score a single batch of stocks with per-stock retry and rate limiting."""
        results = []
        for code, name in batch:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    result = self._score_stock(
                        code, name, trend_analyzer, ScreenerScorer,
                        cache_only=cache_only, df_index=df_index,
                    )
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

    def _score_stock(self, code, name, trend_analyzer, ScreenerScorer,
                     cache_only: bool = False, df_index=None):
        """Fetch data from cache (preferred) or live, then score.

        Args:
            cache_only: When True, skip live fetch fallback and return None
                        if cache is insufficient. Use for full-market scans
                        to avoid per-stock live requests to blocked endpoints.
            df_index:   大盘指数 DataFrame（date + close），用于 RS 计算。
                        None 时 RS 降级为中性值。
        """
        # 1. Try cache first
        df = self._db.get_market_cache_for_stock(code, days=30)

        if df is None or df.empty or len(df) < 5:
            if cache_only:
                return None
            # 2. Fallback to live fetch (for personal stock pool only)
            if self._fetcher is not None:
                df, _ = self._fetcher.get_daily_data(code, days=30)
            if df is None or df.empty or len(df) < 5:
                return None

        # 3. Rename columns if needed — cache uses: open, high, low, close, volume, amount
        # TrendAnalyzer expects: standard OHLCV DataFrame with date index or date column
        if 'trade_date' in df.columns and 'date' not in df.columns:
            df = df.rename(columns={'trade_date': 'date'})

        # 4. Technical analysis (pass df_index for RS calculation when available)
        trend_result = trend_analyzer.analyze(df, code, df_index=df_index)

        # 5. PE/PB from market cache (latest row already loaded in df)
        import math as _math

        def _safe_ratio(series, col):
            if col not in series.index:
                return None
            try:
                v = float(series[col])
                return None if _math.isnan(v) or v <= 0 else v
            except (TypeError, ValueError):
                return None

        latest = df.iloc[-1] if not df.empty else None
        pe_val = _safe_ratio(latest, 'pe_ratio') if latest is not None else None
        pb_val = _safe_ratio(latest, 'pb_ratio') if latest is not None else None

        # PE hard filter: skip stocks with PE > 50 (unknown PE passes through)
        if pe_val is not None and pe_val > 50:
            logger.debug("跳过 %s：PE=%.1f > 50", code, pe_val)
            return None

        # 6. Fundamental data (ROE, gross_margin, etc.) from DB snapshot or live adapter
        financial_report = {}
        dividend_yield = None
        try:
            snap = self._db.get_latest_fundamental_snapshot_by_code(code)
            if snap:
                if snap.get("earnings"):
                    financial_report = snap["earnings"].get("financial_report", {}) or {}
                    div = snap["earnings"].get("dividend") or {}
                    dividend_yield = div.get("ttm_dividend_yield_pct")
            elif not cache_only:
                from data_provider.fundamental_adapter import AkshareFundamentalAdapter
                adapter = AkshareFundamentalAdapter()
                ctx = adapter.get_fundamental_bundle(code)
                if ctx:
                    if ctx.get("earnings"):
                        financial_report = ctx["earnings"].get("financial_report", {}) or {}
                        div = ctx["earnings"].get("dividend") or {}
                        dividend_yield = div.get("ttm_dividend_yield_pct")
        except Exception as e:
            logger.debug("获取 %s 基本面数据失败（跳过）: %s", code, e)

        # Merge PE/PB into financial_report for scoring
        merged = {**financial_report,
                  "pe_ratio": pe_val,
                  "pb_ratio": pb_val}

        return ScreenerScorer.score(code, name, trend_result, merged, dividend_yield)

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
            pe_str = f"PE:{r.pe_ratio:.1f}" if r.pe_ratio is not None and r.pe_ratio > 0 else "PE:N/A"
            pb_str = f"PB:{r.pb_ratio:.1f}" if r.pb_ratio is not None and r.pb_ratio > 0 else "PB:N/A"
            dy_str = f"股息:{r.dividend_yield:.1f}%" if r.dividend_yield is not None and r.dividend_yield > 0 else "股息:N/A"
            lines.append(
                f"{medals[i]} {i + 1}. {r.stock_code} {r.stock_name} | 综合 {r.total_score:.0f}分"
            )
            lines.append(
                f"   技术 {r.tech_score:.0f} | 财务 {r.fund_score:.0f} | {pe_str} {pb_str} {dy_str}"
            )
            lines.append(f"   {reasons_str}")

        self._notify("\n".join(lines))

    # ------------------------------------------------------------------
    # Tracking
    # ------------------------------------------------------------------

    def _run_tracking_report(self, today: datetime.date) -> None:
        """
        用今日市场缓存填充上次 Top10 的次日价格，并推送追踪报告。
        若无待追踪记录则静默跳过。
        """
        try:
            filled = self._db.fill_tracking_prices(today)
            if filled == 0:
                return

            # 找到刚刚被填充的推荐日期（最近一条 tracking_date=today 的 recommend_date）
            report = self._build_tracking_report(today)
            if report:
                self._notify(report)
        except Exception as e:
            logger.warning("追踪报告生成失败: %s", e)

    def _build_tracking_report(self, tracking_date: datetime.date) -> str:
        """生成 Tracking 日期对应的推荐追踪报告文本。"""
        # 找所有 tracking_date=today 的记录（可能跨多个 recommend_date）
        from sqlalchemy import and_
        from src.storage import ScreenerTracking

        rows = []
        try:
            with self._db.get_session() as session:
                rows = (
                    session.query(ScreenerTracking)
                    .filter_by(tracking_date=tracking_date)
                    .order_by(
                        ScreenerTracking.recommend_date.desc(),
                        ScreenerTracking.rank,
                    )
                    .all()
                )
                rows = [
                    {
                        "recommend_date": r.recommend_date,
                        "rank": r.rank,
                        "stock_code": r.stock_code,
                        "stock_name": r.stock_name,
                        "total_score": r.total_score,
                        "change_pct": r.change_pct,
                        "is_accurate": r.is_accurate,
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.warning("读取追踪记录失败: %s", e)
            return ""

        if not rows:
            return ""

        # 按 recommend_date 分组
        from collections import defaultdict
        groups: dict = defaultdict(list)
        for row in rows:
            groups[row["recommend_date"]].append(row)

        lines = [f"📈 Top10 追踪报告（追踪日：{tracking_date}）", ""]
        total_valid = 0
        total_accurate = 0

        for rec_date in sorted(groups.keys(), reverse=True):
            group = groups[rec_date]
            accurate = [r for r in group if r["is_accurate"] is True]
            inaccurate = [r for r in group if r["is_accurate"] is False]
            unknown = [r for r in group if r["is_accurate"] is None]
            valid = len(accurate) + len(inaccurate)
            accuracy = len(accurate) / valid * 100 if valid > 0 else None
            total_valid += valid
            total_accurate += len(accurate)

            acc_str = f"{accuracy:.0f}%" if accuracy is not None else "N/A"
            lines.append(f"📅 推荐日：{rec_date}  准确率：{acc_str} ({len(accurate)}/{valid})")

            for r in group:
                chg = r["change_pct"]
                chg_str = f"{chg:+.2f}%" if chg is not None else "N/A"
                if r["is_accurate"] is True:
                    icon = "✅"
                elif r["is_accurate"] is False:
                    icon = "❌"
                else:
                    icon = "❓"
                lines.append(
                    f"  {icon} {r['rank']}. {r['stock_code']} {r['stock_name']} "
                    f"| {chg_str} | 评分:{r['total_score']:.0f}"
                )
            lines.append("")

        if total_valid > 0 and len(groups) > 1:
            overall = total_accurate / total_valid * 100
            lines.append(f"📊 综合准确率：{overall:.0f}% ({total_accurate}/{total_valid})")

        return "\n".join(lines)
