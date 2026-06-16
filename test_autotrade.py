"""Tests for the refined autotrader ruleset (2026-06-16).

Covers the longs-only / score-threshold entry gate. The edge in the demo data
lived entirely in the long mean-reversion signal, so the default behaviour must
take longs at score >= +threshold and never short.
"""
import os

# Importing main has import-time side effects only on env that is already set in
# CI/dev; the gate helper itself is pure.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from main import _autotrade_side


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
