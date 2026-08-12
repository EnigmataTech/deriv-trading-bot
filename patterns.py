"""Swing-point and double-top/double-bottom (M/W) reversal pattern detection.

A different paradigm from indicators.py's trend-continuation stack (RSI/MACD/
BB/EMA/ADX all trade *with* an established trend). W and M patterns are
reversal setups, so they need real OHLC highs/lows for reliable swing-point
detection — unlike the rest of the signal engine, which works off closes only.

Detection only. This module places no trades and is not wired into
_compute_market_signal — see backtest_patterns.py for historical validation
against real M15 data before this ever touches live/demo execution.
"""
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class SwingPoint:
    index: int
    price: float
    kind: str  # "high" | "low"


def find_swing_points(highs: List[float], lows: List[float], order: int = 3) -> List[SwingPoint]:
    """Fractal-style swing points: bar i is a swing low if lows[i] is the
    strict minimum among lows[i-order : i+order+1] (mirrored for highs).
    A swing point at index i can only be confirmed once `order` bars of
    future data exist past it — that lag is real and unavoidable, not a bug.
    order=3 is a reasonable default for M15 (~roughly a multi-hour pivot)."""
    n = len(highs)
    points: List[SwingPoint] = []
    for i in range(order, n - order):
        window_low = lows[i - order : i + order + 1]
        window_high = highs[i - order : i + order + 1]
        if lows[i] == min(window_low) and window_low.count(lows[i]) == 1:
            points.append(SwingPoint(i, lows[i], "low"))
        if highs[i] == max(window_high) and window_high.count(highs[i]) == 1:
            points.append(SwingPoint(i, highs[i], "high"))
    points.sort(key=lambda p: p.index)
    return points


@dataclass
class PatternResult:
    pattern: Optional[str] = None  # "W" | "M" | None
    neckline: Optional[float] = None
    p1: Optional[SwingPoint] = None
    p2: Optional[SwingPoint] = None
    middle: Optional[SwingPoint] = None
    confirmed: bool = False
    target: Optional[float] = None  # measured-move target once confirmed
    detail: str = ""


def detect_double_bottom(points: List[SwingPoint], current_price: float,
                          tolerance_pct: float = 0.5) -> PatternResult:
    lows = [p for p in points if p.kind == "low"]
    if len(lows) < 2:
        return PatternResult(detail="not enough swing lows")
    low1, low2 = lows[-2], lows[-1]  # two most recent swing lows, in order
    highs_between = [p for p in points if p.kind == "high" and low1.index < p.index < low2.index]
    if not highs_between:
        return PatternResult(detail="no swing high between the two lows")
    neckline_pt = max(highs_between, key=lambda p: p.price)
    spread_pct = abs(low2.price - low1.price) / low1.price * 100
    if spread_pct > tolerance_pct:
        return PatternResult(detail=f"lows {spread_pct:.2f}% apart, exceeds {tolerance_pct}% tolerance")
    confirmed = current_price > neckline_pt.price
    depth = neckline_pt.price - min(low1.price, low2.price)
    return PatternResult(
        pattern="W",
        neckline=neckline_pt.price, p1=low1, p2=low2, middle=neckline_pt,
        confirmed=confirmed,
        target=round(neckline_pt.price + depth, 5) if confirmed else None,
        detail=(f"double bottom {low1.price:.2f}/{low2.price:.2f} "
                f"(neckline {neckline_pt.price:.2f}) — "
                f"{'confirmed, price broke above neckline' if confirmed else 'forming, awaiting neckline break'}"),
    )


def detect_double_top(points: List[SwingPoint], current_price: float,
                       tolerance_pct: float = 0.5) -> PatternResult:
    highs = [p for p in points if p.kind == "high"]
    if len(highs) < 2:
        return PatternResult(detail="not enough swing highs")
    high1, high2 = highs[-2], highs[-1]
    lows_between = [p for p in points if p.kind == "low" and high1.index < p.index < high2.index]
    if not lows_between:
        return PatternResult(detail="no swing low between the two highs")
    neckline_pt = min(lows_between, key=lambda p: p.price)
    spread_pct = abs(high2.price - high1.price) / high1.price * 100
    if spread_pct > tolerance_pct:
        return PatternResult(detail=f"highs {spread_pct:.2f}% apart, exceeds {tolerance_pct}% tolerance")
    confirmed = current_price < neckline_pt.price
    depth = max(high1.price, high2.price) - neckline_pt.price
    return PatternResult(
        pattern="M",
        neckline=neckline_pt.price, p1=high1, p2=high2, middle=neckline_pt,
        confirmed=confirmed,
        target=round(neckline_pt.price - depth, 5) if confirmed else None,
        detail=(f"double top {high1.price:.2f}/{high2.price:.2f} "
                f"(neckline {neckline_pt.price:.2f}) — "
                f"{'confirmed, price broke below neckline' if confirmed else 'forming, awaiting neckline break'}"),
    )


def analyze_pattern(highs: List[float], lows: List[float], current_price: float,
                     order: int = 3, tolerance_pct: float = 0.5) -> PatternResult:
    """Check for both W and M off the same swing points; return whichever
    pattern's second pivot is more recent (the more currently-relevant one)."""
    points = find_swing_points(highs, lows, order)
    w = detect_double_bottom(points, current_price, tolerance_pct)
    m = detect_double_top(points, current_price, tolerance_pct)
    if w.pattern and m.pattern:
        return w if w.p2.index >= m.p2.index else m
    return w if w.pattern else m
