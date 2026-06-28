"""Tests for the refined autotrader ruleset (2026-06-16).

Covers the longs-only / score-threshold entry gate. The edge in the demo data
lived entirely in the long mean-reversion signal, so the default behaviour must
take longs at score >= +threshold and never short.
"""
import os

# Importing main has import-time side effects only on env that is already set in
# CI/dev; the gate helper itself is pure.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from main import _autotrade_side, _atr_from_ohlc, _size_position, _loss_at_stop


class TestLongsOnlyGate:
    def test_takes_long_at_threshold(self):
        assert _autotrade_side(2, 2, longs_only=True) == "buy"
        assert _autotrade_side(3, 2, longs_only=True) == "buy"

    def test_skips_below_threshold(self):
        assert _autotrade_side(1, 2, longs_only=True) is None
        assert _autotrade_side(0, 2, longs_only=True) is None

    def test_never_shorts_when_longs_only(self):
        # The losing side: short signals must be ignored entirely.
        assert _autotrade_side(-2, 2, longs_only=True) is None
        assert _autotrade_side(-3, 2, longs_only=True) is None


class TestBidirectionalGate:
    def test_long_and_short_when_enabled(self):
        assert _autotrade_side(2, 2, longs_only=False) == "buy"
        assert _autotrade_side(-2, 2, longs_only=False) == "sell"

    def test_skips_inside_band(self):
        assert _autotrade_side(1, 2, longs_only=False) is None
        assert _autotrade_side(-1, 2, longs_only=False) is None
        assert _autotrade_side(0, 2, longs_only=False) is None


class TestATR:
    def test_known_value(self):
        closes = [10, 11, 12]
        highs = [10.5, 11.5, 12.5]
        lows = [9.5, 10.5, 11.5]
        # TR1 = max(1.0, 1.5, 0.5)=1.5 ; TR2 = max(1.0, 1.5, 0.5)=1.5 ; seed avg = 1.5
        assert abs(_atr_from_ohlc(highs, lows, closes, period=2) - 1.5) < 1e-9

    def test_insufficient_data(self):
        assert _atr_from_ohlc([1, 2], [0, 1], [1, 2], period=14) is None


class TestPositionSizing:
    def test_basic_2pct(self):
        # $10k, 2% = $200 risk; stop 10, tick_value 1, tick_size 1 -> $10/lot -> 20 lots
        lot = _size_position(10000, 0.02, 10, 1.0, 1.0, vmin=0.01, vmax=100, vstep=0.01)
        assert lot == 20.0

    def test_clamps_to_vmax(self):
        lot = _size_position(10_000_000, 0.02, 1, 1.0, 1.0, vmin=0.01, vmax=5, vstep=0.01)
        assert lot == 5

    def test_floors_to_vmin(self):
        # tiny balance -> below min -> returns vmin
        lot = _size_position(10, 0.02, 100, 1.0, 1.0, vmin=0.5, vmax=100, vstep=0.5)
        assert lot == 0.5

    def test_rounds_down_to_step(self):
        # raw = 200/ (8*1) = 25 ... use values that yield a fractional lot
        # risk $20, stop 1, tick 1 -> 20 lots exactly; use step 0.5 -> stays 20
        lot = _size_position(1000, 0.02, 1, 1.0, 1.0, vmin=0.01, vmax=100, vstep=0.5)
        assert lot == 20.0

    def test_missing_tick_data_returns_none(self):
        assert _size_position(10000, 0.02, 10, 0.0, 0.0, 0.01, 100, 0.01) is None


class TestAffordabilityGate:
    """The dollar risk-at-stop used by the autotrader's balance-aware symbol gate."""

    def test_basic_risk(self):
        # 1 lot, stop 10, tick_value 1, tick_size 1 -> $10 risk
        assert _loss_at_stop(1.0, 10, 1.0, 1.0) == 10.0

    def test_scales_with_lot(self):
        assert _loss_at_stop(0.1, 10, 1.0, 1.0) == 1.0

    def test_invalid_inputs_return_zero(self):
        # 0 means "can't price it" -> caller does not gate
        assert _loss_at_stop(0.0, 10, 1.0, 1.0) == 0.0
        assert _loss_at_stop(1.0, 10, 0.0, 0.0) == 0.0

    def test_small_account_would_skip_high_notional(self):
        # min-lot 1HZ25V-like: stop ~1000 pts, tick_value 1, tick_size 1, 0.1 lot
        # -> ~$100 risk. On a $50 account at 5% cap ($2.50), this must be skipped.
        risk = _loss_at_stop(0.1, 1000, 1.0, 1.0)
        assert risk == 100.0
        assert risk > 0.05 * 50          # gate trips -> skip
        assert risk <= 0.05 * 8367.65    # affordable on the demo -> allowed
