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


class TestFundamentalScore:
    def test_high_quality_financials_score_high(self):
        report = {"roe": 25.0, "gross_margin": 55.0,
                  "revenue_yoy": 35.0, "net_profit_yoy": 40.0, "debt_to_assets": 30.0}
        score = ScreenerScorer.score_fundamental(report)
        assert score >= 85

    def test_poor_financials_score_low(self):
        # PE/PB unknown → neutral(50) adds ~15pts; threshold is 30 not 25
        report = {"roe": 2.0, "gross_margin": 10.0,
                  "revenue_yoy": -5.0, "net_profit_yoy": -10.0, "debt_to_assets": 80.0}
        score = ScreenerScorer.score_fundamental(report)
        assert score <= 30

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
