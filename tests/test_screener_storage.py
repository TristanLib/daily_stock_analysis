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


class TestGetLatestTrackingDate:
    def setup_method(self):
        self.db = DatabaseManager.__new__(DatabaseManager)
        self.db._initialized = False
        self.db.__init__(db_url="sqlite:///:memory:")

    def _insert_tracking_row(self, recommend_date, tracking_date):
        from src.storage import ScreenerTracking
        with self.db.get_session() as session:
            session.add(ScreenerTracking(
                recommend_date=recommend_date,
                stock_code="600519",
                stock_name="贵州茅台",
                rank=1,
                total_score=90.0,
                ref_close=1800.0,
                tracking_date=tracking_date,
                close_price=1820.0 if tracking_date else None,
                change_pct=1.1 if tracking_date else None,
                is_accurate=True if tracking_date else None,
            ))
            session.commit()

    def test_returns_none_when_no_tracking(self):
        result = self.db.get_latest_tracking_date()
        assert result is None

    def test_returns_latest_tracking_date(self):
        d1 = datetime.date(2026, 4, 22)
        d2 = datetime.date(2026, 4, 23)
        self._insert_tracking_row(datetime.date(2026, 4, 21), d1)
        self._insert_tracking_row(datetime.date(2026, 4, 22), d2)
        result = self.db.get_latest_tracking_date()
        assert result == d2

    def test_ignores_unfilled_records(self):
        from src.storage import ScreenerTracking
        filled_date = datetime.date(2026, 4, 22)
        self._insert_tracking_row(datetime.date(2026, 4, 21), filled_date)
        with self.db.get_session() as session:
            session.add(ScreenerTracking(
                recommend_date=datetime.date(2026, 4, 23),
                stock_code="000001",
                stock_name="平安银行",
                rank=1,
                total_score=75.0,
                tracking_date=None,
            ))
            session.commit()
        result = self.db.get_latest_tracking_date()
        assert result == filled_date
