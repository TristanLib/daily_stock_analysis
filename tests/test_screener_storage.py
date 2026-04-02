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
