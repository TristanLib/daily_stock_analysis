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
from typing import Any, Dict, List


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
