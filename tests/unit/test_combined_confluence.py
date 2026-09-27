"""combined_analysis confluence logic (fixed 2026-09-27).

The old logic compared "did today's candle close up" with sentiment, so a
healthy uptrend with one red day was reported as conflicting with bullish
news, and bearish-trend + bearish-news was reported as HIGH agreement.
"""
from __future__ import annotations

import pytest

from tradingview_mcp.server import _compute_confluence


def _tech(trend, momentum="Bearish", signal="BUY"):
    return {
        "market_structure": {"trend": trend},
        "market_sentiment": {"momentum": momentum, "buy_sell_signal": signal},
    }


def _sent(score, label="Strongly Bullish", posts=3):
    return {"sentiment_score": score, "sentiment_label": label, "posts_analyzed": posts}


def test_uptrend_with_red_day_and_bullish_news_is_high_bullish():
    """The exact AMRX case from 27 Sep 2026: bullish structure, red candle,
    bullish news. Must be HIGH/BULLISH, not MIXED."""
    c = _compute_confluence(_tech("Bullish", momentum="Bearish"), _sent(0.597))
    assert c["confidence"] == "HIGH"
    assert c["direction"] == "BULLISH"
    assert c["signals_agree"] is True
    assert "confirmed by" in c["recommendation"]


def test_downtrend_with_bearish_news_is_high_but_bearish():
    """Agreement on the downside is reported as HIGH with direction BEARISH,
    so a buy-side gate that requires BULLISH can't be passed by it."""
    c = _compute_confluence(_tech("Bearish", momentum="Bullish"), _sent(-0.4, "Bearish"))
    assert c["confidence"] == "HIGH"
    assert c["direction"] == "BEARISH"


def test_opposite_directions_are_mixed():
    c = _compute_confluence(_tech("Bullish"), _sent(-0.3, "Bearish"))
    assert c["confidence"] == "MIXED"
    assert c["direction"] is None
    assert c["signals_agree"] is False
    assert "conflicts with" in c["recommendation"]


@pytest.mark.parametrize("trend,score", [
    ("Neutral/Ranging", 0.5),   # no clear technical trend
    ("Bullish", 0.05),          # sentiment inside the neutral band
    ("Bullish", -0.05),
    ("", 0.5),                  # technical payload missing a trend
])
def test_no_clear_direction_is_low(trend, score):
    c = _compute_confluence(_tech(trend), _sent(score))
    assert c["confidence"] == "LOW"
    assert c["direction"] is None
    assert c["signals_agree"] is False


def test_handles_error_payloads():
    """analyze_coin / sentiment can return error dicts; never raise."""
    c = _compute_confluence({"error": "upstream"}, {"error": "no key"})
    assert c["confidence"] == "LOW"
    c = _compute_confluence(None, None)
    assert c["confidence"] == "LOW"
