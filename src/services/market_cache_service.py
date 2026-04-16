# -*- coding: utf-8 -*-
"""
全市场日线缓存服务

职责:
1. update_today()  - 收盘后用 spot_em() 批量拉取当日全市场 OHLCV，写入缓存
2. bootstrap()     - 首次运行时用 Pytdx 补齐历史 K 线数据
3. is_bootstrapped() - 检查缓存是否有足够历史数据
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)

# ETF 前缀（6位代码前两位）
_ETF_PREFIXES = {"51", "52", "56", "58", "15", "16", "18"}


def _safe_float(v):
    """安全转换为 float，NaN 返回 None"""
    try:
        f = float(v)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


def _is_filtered(code: str, name: str) -> bool:
    """
    判断是否应跳过该股票

    过滤规则：
    - 代码以 '8' 开头（北交所）
    - 名称含 'ST' 或 '*ST'
    - 代码前两位属于 ETF 前缀
    """
    code = str(code)
    if code.startswith("8"):
        return True
    if "ST" in str(name):
        return True
    if len(code) >= 2 and code[:2] in _ETF_PREFIXES:
        return True
    return False


class MarketCacheService:
    """全市场日线缓存服务"""

    def __init__(self, db=None):
        if db is None:
            from src.storage import get_db
            db = get_db()
        self._db = db

    # ------------------------------------------------------------------
    # update_today
    # ------------------------------------------------------------------

    def update_today(self, trade_date: date = None) -> int:
        """
        用 akshare spot_em() 拉取当日全市场 OHLCV 并写入缓存。

        Args:
            trade_date: 交易日期，默认为今天

        Returns:
            写入记录数

        Raises:
            Exception: spot_em() 连续失败 2 次后抛出
        """
        if trade_date is None:
            trade_date = date.today()

        import akshare as ak

        # 最多重试 2 次
        last_exc = None
        for attempt in range(2):
            try:
                df = ak.stock_zh_a_spot_em()
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(f"spot_em() 第 {attempt + 1} 次调用失败: {exc}")
        else:
            logger.error(f"spot_em() 连续失败，放弃: {last_exc}")
            raise last_exc

        records = []
        for _, row in df.iterrows():
            code = str(row.get("代码", ""))
            name = str(row.get("名称", ""))
            if _is_filtered(code, name):
                continue
            records.append(
                {
                    "stock_code": code,
                    "stock_name": name,
                    "open": _safe_float(row.get("今开")),
                    "high": _safe_float(row.get("最高")),
                    "low": _safe_float(row.get("最低")),
                    "close": _safe_float(row.get("最新价")),
                    "volume": _safe_float(row.get("成交量")),
                    "amount": _safe_float(row.get("成交额")),
                    "change_pct": _safe_float(row.get("涨跌幅")),
                    "turnover_rate": _safe_float(row.get("换手率")),
                    "volume_ratio": _safe_float(row.get("量比")),
                }
            )

        count = self._db.upsert_market_daily_cache(records, trade_date)
        deleted = self._db.cleanup_old_market_cache(keep_days=35)
        logger.info(
            f"update_today({trade_date}): 写入 {count} 条，清理旧数据 {deleted} 条"
        )
        return count

    # ------------------------------------------------------------------
    # bootstrap
    # ------------------------------------------------------------------

    def bootstrap(self, days: int = 30, notify_fn=None) -> dict:
        """
        用 AkShare stock_zh_a_hist() 补齐历史 K 线缓存（首次运行或缓存为空时使用）。
        使用 AkShare 而非 Pytdx，确保境外服务器也可访问。

        Args:
            days:      向前补齐的交易日数量（实际用 days*2 日历天作为 buffer）
            notify_fn: 可选回调 fn(text)，用于推送进度通知（如 Telegram）

        Returns:
            {"stocks_processed": N, "dates_cached": M, "errors": K}
        """
        import akshare as ak
        import pandas as pd

        # 1. 获取全市场 A 股代码清单
        stocks = self._fetch_universe(ak)
        total = len(stocks)
        logger.info(f"bootstrap: 共 {total} 只股票待处理")
        if notify_fn:
            try:
                notify_fn(f"📦 全市场 Bootstrap 开始\n共 {total} 只股票，预计 10-30 分钟...")
            except Exception:
                pass

        # 2. 计算日期范围
        end_date = date.today()
        start_date = end_date - timedelta(days=days * 2)
        start_str = start_date.strftime("%Y%m%d")
        end_str = end_date.strftime("%Y%m%d")

        # 3. 并发抓取（AkShare stock_zh_a_hist，境外可访问）
        # 限制线程数为 3，避免东财接口并发过高触发封禁
        date_records: dict = defaultdict(list)
        errors = 0
        processed = 0

        def _fetch_one(item):
            code, name = item
            try:
                df = ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_str,
                    end_date=end_str,
                    adjust="qfq",
                )
                if df is None or df.empty:
                    return []
                rows = []
                for _, row in df.iterrows():
                    try:
                        trade_dt = row.get("日期") or row.get("date")
                        if trade_dt is None:
                            continue
                        if hasattr(trade_dt, "date"):
                            trade_dt = trade_dt.date()
                        else:
                            trade_dt = pd.to_datetime(trade_dt).date()
                        rows.append((
                            trade_dt,
                            {
                                "stock_code": code,
                                "stock_name": name,
                                "open":         _safe_float(row.get("开盘") or row.get("open")),
                                "high":         _safe_float(row.get("最高") or row.get("high")),
                                "low":          _safe_float(row.get("最低") or row.get("low")),
                                "close":        _safe_float(row.get("收盘") or row.get("close")),
                                "volume":       _safe_float(row.get("成交量") or row.get("volume")),
                                "amount":       _safe_float(row.get("成交额") or row.get("amount")),
                                "change_pct":   _safe_float(row.get("涨跌幅")),
                                "turnover_rate": _safe_float(row.get("换手率")),
                                "volume_ratio": None,
                            },
                        ))
                    except Exception as row_exc:
                        logger.debug(f"bootstrap row parse error {code}: {row_exc}")
                return rows
            except Exception as exc:
                logger.debug(f"bootstrap fetch error {code}: {exc}")
                return None

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(_fetch_one, item): item for item in stocks}
            for future in as_completed(futures):
                result = future.result()
                processed += 1
                if result is None:
                    errors += 1
                else:
                    for trade_dt, rec in result:
                        date_records[trade_dt].append(rec)
                if processed % 500 == 0:
                    msg = (f"bootstrap 进度: {processed}/{total}，错误: {errors}")
                    logger.info(msg)
                    if notify_fn:
                        try:
                            notify_fn(f"📦 Bootstrap 进度: {processed}/{total} 只（错误 {errors}）")
                        except Exception:
                            pass

        # 4. 按日期写入 DB
        dates_cached = 0
        for trade_dt in sorted(date_records.keys()):
            day_records = date_records[trade_dt]
            if day_records:
                self._db.upsert_market_daily_cache(day_records, trade_dt)
                dates_cached += 1

        summary = (
            f"bootstrap 完成: 处理 {processed} 只股票，"
            f"缓存 {dates_cached} 个交易日，错误 {errors} 只"
        )
        logger.info(summary)
        if notify_fn:
            try:
                notify_fn(f"✅ Bootstrap 完成\n{processed} 只股票 / {dates_cached} 个交易日 / {errors} 只失败")
            except Exception:
                pass
        return {
            "stocks_processed": processed,
            "dates_cached": dates_cached,
            "errors": errors,
        }

    def _fetch_universe(self, ak) -> list:
        """
        从 akshare 获取沪深两市 A 股代码与名称清单，去重并过滤。

        Returns:
            [(code, name), ...]
        """
        seen = set()
        result = []

        def _add(df, code_col, name_col):
            if df is None or df.empty:
                return
            for _, row in df.iterrows():
                code = str(row.get(code_col, "")).strip().zfill(6)
                name = str(row.get(name_col, "")).strip()
                if code in seen:
                    continue
                seen.add(code)
                if _is_filtered(code, name):
                    continue
                result.append((code, name))

        try:
            sh_df = ak.stock_info_sh_name_code(symbol="主板A股")
            _add(sh_df, "证券代码", "证券简称")
        except Exception as exc:
            logger.warning(f"获取上证 A 股清单失败: {exc}")

        try:
            sz_df = ak.stock_info_sz_name_code(symbol="A股列表")
            _add(sz_df, "A股代码", "A股简称")
        except Exception as exc:
            logger.warning(f"获取深证 A 股清单失败: {exc}")

        logger.info(f"_fetch_universe: 共 {len(result)} 只股票（过滤后）")
        return result

    # ------------------------------------------------------------------
    # is_bootstrapped
    # ------------------------------------------------------------------

    def is_bootstrapped(self, min_days: int = 20) -> bool:
        """
        检查缓存是否有足够的历史数据。

        Args:
            min_days: 最少要求的不同交易日数量

        Returns:
            True 表示已有足够历史
        """
        universe = self._db.get_cached_universe()
        if not universe:
            return False

        with self._db.session_scope() as session:
            from src.storage import MarketDailyCache
            from sqlalchemy import func, select as sa_select

            count = (
                session.execute(
                    sa_select(func.count(func.distinct(MarketDailyCache.trade_date)))
                ).scalar()
                or 0
            )

        return count >= min_days
