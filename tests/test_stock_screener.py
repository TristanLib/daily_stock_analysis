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
