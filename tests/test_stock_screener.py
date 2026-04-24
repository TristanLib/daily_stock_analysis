# tests/test_stock_screener.py
import datetime
from unittest.mock import MagicMock
from src.services.stock_screener import StockScreener


class TestStockUniverse:
    def test_get_sh_stock_universe_returns_6digit_codes(self):
        import pandas as pd
        mock_df = pd.DataFrame({
            "代码": ["600519", "600000", "601318"],
            "名称": ["贵州茅台", "浦发银行", "中国平安"],
        })
        screener = StockScreener.__new__(StockScreener)
        codes = screener._parse_universe_df(mock_df)
        assert codes == [("600519", "贵州茅台"), ("600000", "浦发银行"), ("601318", "中国平安")]

    def test_universe_filters_non_6digit(self):
        import pandas as pd
        mock_df = pd.DataFrame({
            "代码": ["600519", "00700", "ABC"],
            "名称": ["贵州茅台", "腾讯控股", "某某"],
        })
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


class TestMorningReview:
    def _make_screener(self, db=None, telegram=None):
        screener = StockScreener.__new__(StockScreener)
        screener._config = MagicMock()
        screener._fetcher = MagicMock()
        screener._db = db or MagicMock()
        screener._telegram = telegram
        return screener

    def test_get_prev_trade_date_returns_latest_cache_date(self):
        from src.storage import DatabaseManager, MarketDailyCache
        db = DatabaseManager.__new__(DatabaseManager)
        db._initialized = False
        db.__init__(db_url="sqlite:///:memory:")
        today = datetime.date(2026, 4, 24)
        prev = datetime.date(2026, 4, 23)
        with db.get_session() as session:
            session.add(MarketDailyCache(
                trade_date=prev, stock_code="600519", stock_name="贵州茅台",
                open=1800.0, high=1850.0, low=1790.0, close=1820.0,
                volume=1000.0, amount=1820000.0, change_pct=1.1,
            ))
            session.commit()
        screener = StockScreener.__new__(StockScreener)
        screener._db = db
        assert screener._get_prev_trade_date(today) == prev

    def test_get_prev_trade_date_returns_none_when_no_cache(self):
        from src.storage import DatabaseManager
        db = DatabaseManager.__new__(DatabaseManager)
        db._initialized = False
        db.__init__(db_url="sqlite:///:memory:")
        screener = StockScreener.__new__(StockScreener)
        screener._db = db
        assert screener._get_prev_trade_date(datetime.date(2026, 4, 24)) is None

    def test_send_morning_reminder_notifies(self):
        from src.services.screener_scorer import ScreenerResult
        sent = []
        mock_telegram = MagicMock()
        mock_telegram.send_to_telegram = lambda msg: sent.append(msg)
        screener = self._make_screener(telegram=mock_telegram)
        top10 = [ScreenerResult("600519", "贵州茅台", 90.0, 85.0, 88.0, ["均线多头"])]
        screener._send_morning_reminder(top10, datetime.date(2026, 4, 23))
        assert len(sent) == 1
        assert "600519" in sent[0]
        assert "今日盘前关注" in sent[0]

    def test_run_morning_review_no_prev_date_skips(self):
        mock_db = MagicMock()
        mock_telegram = MagicMock()
        screener = self._make_screener(db=mock_db, telegram=mock_telegram)
        screener._get_prev_trade_date = lambda today: None
        screener.run_morning_review()
        mock_telegram.send_to_telegram.assert_not_called()

    def test_run_morning_review_sends_two_notifications(self):
        from src.services.screener_scorer import ScreenerResult
        sent = []
        mock_telegram = MagicMock()
        mock_telegram.send_to_telegram = lambda msg: sent.append(msg)

        mock_db = MagicMock()
        mock_db.get_latest_tracking_date.return_value = datetime.date(2026, 4, 23)
        mock_db.get_top_screener_results.return_value = [
            ScreenerResult("600519", "贵州茅台", 90.0, 85.0, 88.0, ["均线多头"]),
        ]

        screener = self._make_screener(db=mock_db, telegram=mock_telegram)
        screener._get_prev_trade_date = lambda today: datetime.date(2026, 4, 23)
        screener._build_tracking_report = lambda d: "📈 追踪报告内容"

        screener.run_morning_review()
        assert len(sent) == 2
        assert "追踪报告" in sent[0]
        assert "今日盘前关注" in sent[1]
