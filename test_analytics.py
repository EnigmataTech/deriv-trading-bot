"""
Synthetic-data unit tests for analytics.py and the new TechnicalIndicators methods.
Run: python test_analytics.py
"""

import math
import sys
import traceback
from datetime import datetime, timedelta
from typing import List

from analytics import (
    ClosedTrade,
    _fmt_duration,
    _median,
    _normal_inv_cdf,
    calculate_per_symbol_stats,
    calculate_portfolio_stats,
    calculate_trade_stats,
    duration_quality_report,
    streak_risk_assessment,
)
from deriv_client import TechnicalIndicators as TI


PASS = 0
FAIL = 0
FAILS: List[str] = []


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILS.append(f"{name}  ({detail})")
        print(f"  FAIL  {name}  -- {detail}")


def approx(a: float, b: float, tol: float = 1e-4) -> bool:
    return abs(a - b) <= tol


def mk_trade(tid: str, sym: str, pnl: float, opened: datetime, closed: datetime,
             ttype: str = "buy", amt: float = 100.0) -> ClosedTrade:
    return ClosedTrade(
        trade_id=tid, symbol=sym, trade_type=ttype, amount=amt,
        profit_loss=pnl, opened_at=opened, closed_at=closed,
    )


# ---------------------------------------------------------------------------
# Internal helper tests
# ---------------------------------------------------------------------------

def test_helpers():
    print("\n[helpers]")
    check("_median empty", _median([]) == 0.0)
    check("_median odd", _median([3, 1, 2]) == 2.0)
    check("_median even", _median([4, 2, 1, 3]) == 2.5)

    check("_fmt_duration seconds", _fmt_duration(45) == "45s")
    check("_fmt_duration minutes", _fmt_duration(150) == "2.5m")
    check("_fmt_duration hours", _fmt_duration(7200) == "2.0h")

    # _normal_inv_cdf reference values
    # Φ⁻¹(0.05) ≈ -1.6449, Φ⁻¹(0.01) ≈ -2.3263, Φ⁻¹(0.95) ≈ +1.6449
    check("inv_cdf 0.05", approx(_normal_inv_cdf(0.05), -1.6449, tol=0.01),
          f"got {_normal_inv_cdf(0.05):.4f}")
    check("inv_cdf 0.01", approx(_normal_inv_cdf(0.01), -2.3263, tol=0.01),
          f"got {_normal_inv_cdf(0.01):.4f}")
    check("inv_cdf 0.95", approx(_normal_inv_cdf(0.95), 1.6449, tol=0.01),
          f"got {_normal_inv_cdf(0.95):.4f}")
    check("inv_cdf 0.5 ≈ 0", approx(_normal_inv_cdf(0.5), 0.0, tol=0.01))


# ---------------------------------------------------------------------------
# Trade-stats with a hand-checked fixture
# ---------------------------------------------------------------------------

def test_trade_stats_known():
    print("\n[calculate_trade_stats — known answers]")
    base = datetime(2026, 5, 1, 10, 0, 0)
    pnls = [10.0, -5.0, 15.0, -8.0]
    trades = []
    for i, pnl in enumerate(pnls):
        opened = base + timedelta(hours=i)
        closed = opened + timedelta(minutes=30)
        trades.append(mk_trade(f"t{i}", "R_100", pnl, opened, closed))

    s = calculate_trade_stats(trades)

    check("total_trades=4", s.total_trades == 4)
    check("wins=2", s.winning_trades == 2)
    check("losses=2", s.losing_trades == 2)
    check("win_rate=0.5", approx(s.win_rate, 0.5))
    check("total_pnl=12", approx(s.total_profit_loss, 12.0))
    check("total_profit=25", approx(s.total_profit, 25.0))
    check("total_loss=-13", approx(s.total_loss, -13.0))
    check("largest_profit=15", approx(s.largest_profit, 15.0))
    check("largest_loss=-8", approx(s.largest_loss, -8.0))
    check("avg_pnl=3", approx(s.average_profit_loss, 3.0))
    check("avg_profit=12.5", approx(s.average_profit, 12.5))
    check("avg_loss=-6.5", approx(s.average_loss, -6.5))

    # profit_factor = 25/13
    check("profit_factor=25/13", approx(s.profit_factor, 25.0 / 13.0, tol=1e-4),
          f"got {s.profit_factor:.4f}")
    # profit_loss_ratio = 12.5/6.5
    check("pl_ratio=12.5/6.5", approx(s.profit_loss_ratio, 12.5 / 6.5, tol=1e-4),
          f"got {s.profit_loss_ratio:.4f}")
    # expectancy = win_rate * pl_ratio - loss_rate
    expected_exp = 0.5 * (12.5 / 6.5) - 0.5
    check("expectancy", approx(s.expectancy, expected_exp, tol=1e-4),
          f"got {s.expectancy:.4f} expected {expected_exp:.4f}")

    # variance via sample formula: data=[10,-5,15,-8], mean=3
    # dev² sum = 49+64+144+121 = 378  → sample var = 378/3 = 126
    expected_std = math.sqrt(126.0)
    check("std_dev ≈ √126", approx(s.profit_loss_std_dev, expected_std, tol=1e-4),
          f"got {s.profit_loss_std_dev:.4f}")
    expected_sharpe = 3.0 / expected_std
    check("trade-level sharpe", approx(s.sharpe_ratio, expected_sharpe, tol=1e-4),
          f"got {s.sharpe_ratio:.4f}")

    # downside dev: losses [-5,-8], mean=-6.5
    # Welford: sum_dv = (-5 - 0)(-5 - -5) + (-8 - -5)(-8 - -6.5) = 0 + 4.5 = 4.5
    # sample var = 4.5 / 1 = 4.5  → std = √4.5
    expected_down = math.sqrt(4.5)
    check("downside_dev ≈ √4.5", approx(s.profit_loss_downside_dev, expected_down, tol=1e-4),
          f"got {s.profit_loss_downside_dev:.4f}")
    check("sortino", approx(s.sortino_ratio, 3.0 / expected_down, tol=1e-4),
          f"got {s.sortino_ratio:.4f}")

    # streaks (alternating w/l/w/l): max_wins=1, max_losses=1, current_losses=1
    check("max_consecutive_wins=1", s.max_consecutive_wins == 1)
    check("max_consecutive_losses=1", s.max_consecutive_losses == 1)
    check("current_consecutive_losses=1", s.current_consecutive_losses == 1)

    # drawdown: cumulative = [10, 5, 20, 12]
    # peaks: 10 (after 1st), then 5 (dd=-5), 20 (new peak), 12 (dd=-8)
    # max_closed_drawdown = -8
    check("max_closed_drawdown=-8", approx(s.max_closed_drawdown, -8.0))
    # profit_to_max_drawdown = 12 / 8 = 1.5
    check("profit/dd ratio=1.5", approx(s.profit_to_max_drawdown_ratio, 1.5))


def test_trade_stats_edge_cases():
    print("\n[calculate_trade_stats — edges]")
    s = calculate_trade_stats([])
    check("empty → total_trades=0", s.total_trades == 0)
    check("empty → all defaults", s.win_rate == 0.0 and s.profit_factor == 0.0)

    base = datetime(2026, 5, 1, 10, 0, 0)
    # All wins → profit_factor falls back to 10.0 sentinel
    all_wins = [mk_trade(f"w{i}", "R_100", 5.0, base + timedelta(hours=i),
                          base + timedelta(hours=i, minutes=30)) for i in range(3)]
    sw = calculate_trade_stats(all_wins)
    check("all wins → profit_factor=10 sentinel", sw.profit_factor == 10.0)
    check("all wins → win_rate=1.0", sw.win_rate == 1.0)
    check("all wins → max_consecutive_wins=3", sw.max_consecutive_wins == 3)

    # Streaks: w,w,w,l,l,w,l,l,l should give max_wins=3, max_losses=3, current=3
    streak_pnls = [1, 1, 1, -1, -1, 1, -1, -1, -1]
    streak_trades = [mk_trade(f"s{i}", "R_100", p, base + timedelta(hours=i),
                              base + timedelta(hours=i, minutes=30))
                     for i, p in enumerate(streak_pnls)]
    ss = calculate_trade_stats(streak_trades)
    check("streak max_wins=3", ss.max_consecutive_wins == 3, f"got {ss.max_consecutive_wins}")
    check("streak max_losses=3", ss.max_consecutive_losses == 3, f"got {ss.max_consecutive_losses}")
    check("streak current_losses=3", ss.current_consecutive_losses == 3,
          f"got {ss.current_consecutive_losses}")


# ---------------------------------------------------------------------------
# Portfolio stats
# ---------------------------------------------------------------------------

def test_portfolio_stats():
    print("\n[calculate_portfolio_stats]")
    # 4 trades on 4 distinct days starting 2026-05-01
    base = datetime(2026, 5, 1, 10, 0, 0)
    pnls = [10.0, -5.0, 15.0, -8.0]
    trades = [mk_trade(f"t{i}", "R_100", p, base + timedelta(days=i),
                       base + timedelta(days=i, hours=1))
              for i, p in enumerate(pnls)]

    ps = calculate_portfolio_stats(trades, starting_equity=1000.0)
    check("start_equity=1000", ps.start_equity == 1000.0)
    check("end_equity=1012", approx(ps.end_equity, 1012.0))
    check("net_profit_pct=1.2", approx(ps.total_net_profit_pct, 1.2))

    # Equity peaks: 1010 (day0), 1005 (dd=-0.495%), 1020 (peak), 1012 (dd=-0.784%)
    # max_drawdown_pct rounded to 2 → -0.78
    check("max_drawdown_pct≈-0.78", approx(ps.max_drawdown_pct, -0.78, tol=0.01),
          f"got {ps.max_drawdown_pct}")

    # Sharpe should be finite and non-zero
    check("sharpe non-zero", ps.sharpe_ratio != 0.0, f"got {ps.sharpe_ratio:.4f}")
    # VaR fields should be populated
    check("var_95 set", ps.var_95 != 0.0, f"got {ps.var_95}")
    check("var_99 set", ps.var_99 != 0.0, f"got {ps.var_99}")

    # Empty case
    empty_ps = calculate_portfolio_stats([], starting_equity=1000.0)
    check("empty trades → start_equity preserved", empty_ps.start_equity == 1000.0)
    check("empty trades → end_equity=0", empty_ps.end_equity == 0.0)


# ---------------------------------------------------------------------------
# Per-symbol breakdown
# ---------------------------------------------------------------------------

def test_per_symbol():
    print("\n[calculate_per_symbol_stats]")
    base = datetime(2026, 5, 1, 10, 0, 0)
    trades = []
    # R_100: 2 wins, 1 loss → win_rate ≈ 0.667
    for i, p in enumerate([5.0, 10.0, -3.0]):
        trades.append(mk_trade(f"a{i}", "R_100", p, base + timedelta(hours=i),
                               base + timedelta(hours=i, minutes=30)))
    # R_50: 1 win, 2 losses → win_rate ≈ 0.333
    for i, p in enumerate([7.0, -4.0, -2.0]):
        trades.append(mk_trade(f"b{i}", "R_50", p, base + timedelta(hours=10 + i),
                               base + timedelta(hours=10 + i, minutes=30)))

    out = calculate_per_symbol_stats(trades)
    check("two symbols returned", len(out) == 2, f"got {len(out)}")

    by_sym = {ss.symbol: ss for ss in out}
    check("R_100 present", "R_100" in by_sym)
    check("R_50 present", "R_50" in by_sym)
    check("R_100 win_rate≈0.667", approx(by_sym["R_100"].win_rate, 2 / 3, tol=1e-3))
    check("R_50 win_rate≈0.333", approx(by_sym["R_50"].win_rate, 1 / 3, tol=1e-3))
    check("R_100 total_pnl=12", approx(by_sym["R_100"].total_profit_loss, 12.0))
    check("R_50 total_pnl=1", approx(by_sym["R_50"].total_profit_loss, 1.0))


# ---------------------------------------------------------------------------
# Streak risk
# ---------------------------------------------------------------------------

def test_streak_risk():
    print("\n[streak_risk_assessment]")
    base = datetime(2026, 5, 1, 10, 0, 0)
    # Build a sequence ending with a 4-loss run; max_historical = 4
    pnls = [1, 1, -1, -1, -1, -1]
    trades = [mk_trade(f"x{i}", "R_100", p, base + timedelta(hours=i),
                       base + timedelta(hours=i, minutes=30))
              for i, p in enumerate(pnls)]
    ts = calculate_trade_stats(trades)
    sr = streak_risk_assessment(ts)
    check("hist_max=4", sr["max_historical_streak"] == 4, str(sr))
    check("current=4", sr["current_consecutive_losses"] == 4, str(sr))
    check("threshold=max(3, round(4*0.75))=3", sr["pause_threshold"] == 3, str(sr))
    check("recommend_pause=True", sr["recommend_pause"] is True)

    # Below threshold case
    below = [mk_trade(f"y{i}", "R_100", 1.0, base + timedelta(hours=i),
                       base + timedelta(hours=i, minutes=30)) for i in range(3)]
    ts2 = calculate_trade_stats(below)
    sr2 = streak_risk_assessment(ts2)
    check("no losses → recommend_pause=False", sr2["recommend_pause"] is False)


# ---------------------------------------------------------------------------
# Duration quality
# ---------------------------------------------------------------------------

def test_duration_quality():
    print("\n[duration_quality_report]")
    base = datetime(2026, 5, 1, 10, 0, 0)
    # 8 trades with durations 60, 120, 180, 240, 300, 360, 420, 480 seconds
    # quartiles: q1=180 (idx 2), q2=300 (idx 4), q3=420 (idx 6)
    trades = []
    for i in range(8):
        secs = (i + 1) * 60
        opened = base + timedelta(hours=i)
        closed = opened + timedelta(seconds=secs)
        # alternate win/loss so each bucket has at least one of each
        pnl = 1.0 if i % 2 == 0 else -1.0
        trades.append(mk_trade(f"d{i}", "R_100", pnl, opened, closed))

    rep = duration_quality_report(trades)
    check("at least 3 buckets populated", len(rep) >= 3, f"got {list(rep.keys())}")
    total = sum(b["trades"] for b in rep.values())
    check("all trades bucketed", total == 8, f"got {total}")
    total_pnl = sum(b["total_pnl"] for b in rep.values())
    check("total bucket pnl=0", approx(total_pnl, 0.0))

    check("empty → {}", duration_quality_report([]) == {})


# ---------------------------------------------------------------------------
# TechnicalIndicators new methods
# ---------------------------------------------------------------------------

def test_indicators():
    print("\n[TechnicalIndicators — new methods]")

    # EMA with constant prices: should equal that constant from index period-1
    const = [100.0] * 20
    ema = TI.calculate_ema(const, 5)
    check("EMA len matches input", len(ema) == len(const))
    check("EMA[0..3] None", all(x is None for x in ema[:4]))
    check("EMA[4]=100 seed", ema[4] == 100.0, f"got {ema[4]}")
    check("EMA[19]=100", ema[19] == 100.0, f"got {ema[19]}")
    # Insufficient data
    short = TI.calculate_ema([1.0, 2.0], 5)
    check("EMA short → all None", all(x is None for x in short))

    # MACD with constant prices: signal & line both 0 in the populated region
    macd_line, signal_line, hist = TI.calculate_macd([100.0] * 60)
    check("MACD lengths match", len(macd_line) == 60 and len(signal_line) == 60)
    nonzero = [v for v in macd_line if v not in (None, 0.0)]
    check("MACD constant prices → 0 line", len(nonzero) == 0,
          f"non-zero: {nonzero[:3]}")
    sig_nonzero = [v for v in signal_line if v not in (None, 0.0)]
    check("MACD constant prices → 0 signal", len(sig_nonzero) == 0)
    # Last hist value should be 0 (both are 0)
    last_hist = next((h for h in reversed(hist) if h is not None), None)
    check("MACD constant prices → 0 hist", last_hist == 0.0, f"got {last_hist}")

    # MACD on a real ramp: should have non-None values from index 25+
    ramp = [100.0 + i for i in range(60)]
    m, sig, h = TI.calculate_macd(ramp)
    check("MACD ramp has signal at end", sig[-1] is not None, f"got {sig[-1]}")
    check("MACD ramp histogram set", h[-1] is not None)

    # Bollinger Bands on constant prices: bands collapse onto price
    upper, middle, lower = TI.calculate_bollinger_bands([100.0] * 30, period=20)
    check("BB[19] middle=100", middle[19] == 100.0)
    check("BB[19] upper=middle", upper[19] == middle[19])
    check("BB[19] lower=middle", lower[19] == middle[19])
    check("BB[0..18] None", all(m is None for m in middle[:19]))

    # BB on a known sequence: prices [1..20], period=20
    # mean = 10.5, population std = sqrt(sum((i-10.5)^2)/20) = sqrt(33.25) ≈ 5.7663
    seq = [float(i) for i in range(1, 21)]
    u, m20, l = TI.calculate_bollinger_bands(seq, period=20, std_mult=2.0)
    check("BB[19] middle=10.5", approx(m20[19], 10.5))
    expected_std = math.sqrt(33.25)
    check("BB[19] upper≈10.5+2σ",
          approx(u[19], 10.5 + 2 * expected_std, tol=1e-3),
          f"got {u[19]}")
    check("BB[19] lower≈10.5-2σ",
          approx(l[19], 10.5 - 2 * expected_std, tol=1e-3),
          f"got {l[19]}")

    # ATR on a ramp (constant +1 step): True Range = 1 each step → ATR = 1.0
    ramp = [100.0 + i for i in range(20)]
    atr = TI.calculate_atr(ramp, period=14)
    check("ATR ramp[14]=1.0", atr[14] == 1.0, f"got {atr[14]}")
    check("ATR ramp[19]=1.0", atr[19] == 1.0, f"got {atr[19]}")
    check("ATR[0..13] None", all(a is None for a in atr[:14]))

    # ATR on constant prices = 0
    atr_const = TI.calculate_atr([50.0] * 20, period=14)
    check("ATR constant=0", atr_const[14] == 0.0 and atr_const[19] == 0.0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    suites = [
        test_helpers,
        test_trade_stats_known,
        test_trade_stats_edge_cases,
        test_portfolio_stats,
        test_per_symbol,
        test_streak_risk,
        test_duration_quality,
        test_indicators,
    ]
    for suite in suites:
        try:
            suite()
        except Exception as e:
            global FAIL
            FAIL += 1
            FAILS.append(f"{suite.__name__} crashed: {e}")
            print(f"\n  CRASH in {suite.__name__}:")
            traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    if FAILS:
        print("\n  Failures:")
        for f in FAILS:
            print(f"    - {f}")
    print("=" * 60)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
