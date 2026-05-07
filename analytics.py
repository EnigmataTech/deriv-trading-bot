"""
Trade analytics — ported from QuantConnect Lean's TradeStatistics and
PortfolioStatistics algorithms, adapted for the Deriv multiplier trade schema.

Sources:
  Common/Statistics/TradeStatistics.cs
  Common/Statistics/PortfolioStatistics.cs
  Common/Statistics/Statistics.cs
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Dict


# ---------------------------------------------------------------------------
# Input / output data structures
# ---------------------------------------------------------------------------

@dataclass
class ClosedTrade:
    trade_id: str
    symbol: str
    trade_type: str        # 'buy' / 'sell'
    amount: float
    profit_loss: float
    opened_at: datetime
    closed_at: datetime

    @property
    def duration(self) -> timedelta:
        return self.closed_at - self.opened_at

    @property
    def is_win(self) -> bool:
        return self.profit_loss > 0


@dataclass
class TradeStats:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0

    total_profit_loss: float = 0.0
    total_profit: float = 0.0
    total_loss: float = 0.0
    largest_profit: float = 0.0
    largest_loss: float = 0.0
    average_profit_loss: float = 0.0
    average_profit: float = 0.0
    average_loss: float = 0.0

    win_rate: float = 0.0
    loss_rate: float = 0.0
    win_loss_ratio: float = 0.0
    profit_loss_ratio: float = 0.0    # avg_win / |avg_loss|
    profit_factor: float = 0.0        # total_profit / |total_loss|
    expectancy: float = 0.0           # win_rate * profit_loss_ratio - loss_rate

    profit_loss_std_dev: float = 0.0
    profit_loss_downside_dev: float = 0.0
    sharpe_ratio: float = 0.0         # avg_pnl / pnl_std_dev  (trade-level)
    sortino_ratio: float = 0.0        # avg_pnl / downside_dev

    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    current_consecutive_losses: int = 0   # live streak at time of calculation

    max_closed_drawdown: float = 0.0
    profit_to_max_drawdown_ratio: float = 0.0

    avg_duration_seconds: float = 0.0
    avg_winning_duration_seconds: float = 0.0
    avg_losing_duration_seconds: float = 0.0
    median_duration_seconds: float = 0.0


@dataclass
class PortfolioStats:
    start_equity: float = 0.0
    end_equity: float = 0.0
    total_net_profit_pct: float = 0.0
    cagr: float = 0.0
    max_drawdown_pct: float = 0.0
    drawdown_recovery_days: int = 0
    sharpe_ratio: float = 0.0         # annualised, equity-curve based
    sortino_ratio: float = 0.0
    var_95: float = 0.0               # 1-day VaR at 95% confidence (as pct of equity)
    var_99: float = 0.0


@dataclass
class SymbolStats:
    symbol: str
    trade_stats: TradeStats

    @property
    def win_rate(self) -> float:
        return self.trade_stats.win_rate

    @property
    def total_profit_loss(self) -> float:
        return self.trade_stats.total_profit_loss

    @property
    def profit_factor(self) -> float:
        return self.trade_stats.profit_factor

    @property
    def sharpe_ratio(self) -> float:
        return self.trade_stats.sharpe_ratio


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _normal_inv_cdf(p: float) -> float:
    """Rational approximation of standard normal inverse CDF (Abramowitz & Stegun 26.2.17)."""
    if p <= 0 or p >= 1:
        return 0.0
    q = min(p, 1 - p)
    t = math.sqrt(-2.0 * math.log(q))
    c = [2.515517, 0.802853, 0.010328]
    d = [1.432788, 0.189269, 0.001308]
    x = t - (c[0] + c[1] * t + c[2] * t * t) / (1 + d[0] * t + d[1] * t * t + d[2] * t * t * t)
    return -x if p < 0.5 else x


# ---------------------------------------------------------------------------
# Trade-level statistics  (ported from TradeStatistics.cs)
# ---------------------------------------------------------------------------

def calculate_trade_stats(trades: List[ClosedTrade]) -> TradeStats:
    s = TradeStats()
    if not trades:
        return s

    trades = sorted(trades, key=lambda t: t.closed_at)

    max_cumulative_pnl = 0.0
    consecutive_wins = 0
    consecutive_losses = 0
    sum_variance = 0.0
    sum_downside_variance = 0.0
    all_durations: List[float] = []
    win_durations: List[float] = []
    loss_durations: List[float] = []

    for trade in trades:
        s.total_trades += 1
        pnl = trade.profit_loss
        dur = trade.duration.total_seconds()

        # Welford online mean + variance for all P&L
        prev_avg = s.average_profit_loss
        s.average_profit_loss += (pnl - prev_avg) / s.total_trades
        sum_variance += (pnl - prev_avg) * (pnl - s.average_profit_loss)
        variance = sum_variance / (s.total_trades - 1) if s.total_trades > 1 else 0.0
        s.profit_loss_std_dev = math.sqrt(variance)

        s.total_profit_loss += pnl
        all_durations.append(dur)
        s.avg_duration_seconds += (dur - s.avg_duration_seconds) / s.total_trades

        if pnl > 0:
            s.winning_trades += 1
            s.total_profit += pnl
            s.average_profit += (pnl - s.average_profit) / s.winning_trades
            win_durations.append(dur)
            s.avg_winning_duration_seconds += (dur - s.avg_winning_duration_seconds) / s.winning_trades
            if pnl > s.largest_profit:
                s.largest_profit = pnl

            consecutive_wins += 1
            consecutive_losses = 0
            if consecutive_wins > s.max_consecutive_wins:
                s.max_consecutive_wins = consecutive_wins
            if s.total_profit_loss > max_cumulative_pnl:
                max_cumulative_pnl = s.total_profit_loss
        else:
            s.losing_trades += 1
            s.total_loss += pnl
            prev_avg_loss = s.average_loss
            s.average_loss += (pnl - s.average_loss) / s.losing_trades
            loss_durations.append(dur)
            s.avg_losing_duration_seconds += (dur - s.avg_losing_duration_seconds) / s.losing_trades
            if pnl < s.largest_loss:
                s.largest_loss = pnl

            # Welford downside variance (losses only)
            sum_downside_variance += (pnl - prev_avg_loss) * (pnl - s.average_loss)
            dv = sum_downside_variance / (s.losing_trades - 1) if s.losing_trades > 1 else 0.0
            s.profit_loss_downside_dev = math.sqrt(dv)

            consecutive_losses += 1
            consecutive_wins = 0
            if consecutive_losses > s.max_consecutive_losses:
                s.max_consecutive_losses = consecutive_losses

            dd = s.total_profit_loss - max_cumulative_pnl
            if dd < s.max_closed_drawdown:
                s.max_closed_drawdown = dd

    s.current_consecutive_losses = consecutive_losses

    s.win_rate = s.winning_trades / s.total_trades if s.total_trades > 0 else 0.0
    s.loss_rate = 1.0 - s.win_rate
    s.profit_loss_ratio = (
        s.average_profit / abs(s.average_loss) if s.average_loss < 0 else 0.0
    )
    s.win_loss_ratio = (
        (s.winning_trades / s.losing_trades) if s.losing_trades > 0
        else (10.0 if s.winning_trades > 0 else 0.0)
    )
    s.profit_factor = (
        (s.total_profit / abs(s.total_loss)) if s.total_loss < 0
        else (10.0 if s.total_profit > 0 else 0.0)
    )
    s.expectancy = s.win_rate * s.profit_loss_ratio - s.loss_rate

    s.sharpe_ratio = (
        s.average_profit_loss / s.profit_loss_std_dev if s.profit_loss_std_dev > 0 else 0.0
    )
    s.sortino_ratio = (
        s.average_profit_loss / s.profit_loss_downside_dev if s.profit_loss_downside_dev > 0 else 0.0
    )
    s.profit_to_max_drawdown_ratio = (
        (s.total_profit_loss / abs(s.max_closed_drawdown)) if s.max_closed_drawdown < 0
        else (10.0 if s.total_profit_loss > 0 else 0.0)
    )
    s.median_duration_seconds = _median(all_durations)

    return s


# ---------------------------------------------------------------------------
# Portfolio-level statistics  (ported from PortfolioStatistics.cs)
# ---------------------------------------------------------------------------

def calculate_portfolio_stats(
    trades: List[ClosedTrade],
    starting_equity: float,
) -> PortfolioStats:
    ps = PortfolioStats(start_equity=starting_equity)
    closed = sorted(trades, key=lambda t: t.closed_at)
    if not closed or starting_equity <= 0:
        return ps

    # Build equity curve from cumulative P&L
    equity = starting_equity
    equity_curve: List[tuple] = []
    for trade in closed:
        equity += trade.profit_loss
        equity_curve.append((trade.closed_at, equity))

    ps.end_equity = equity_curve[-1][1]
    ps.total_net_profit_pct = (ps.end_equity / starting_equity - 1.0) * 100.0

    span_days = (equity_curve[-1][0] - equity_curve[0][0]).total_seconds() / 86400.0
    if span_days > 0 and ps.end_equity > 0:
        years = span_days / 365.25
        if years > 0:
            ps.cagr = ((ps.end_equity / starting_equity) ** (1.0 / years) - 1.0) * 100.0

    # Aggregate to daily equity for return series
    daily: Dict[str, float] = {}
    for ts, eq in equity_curve:
        daily[ts.strftime("%Y-%m-%d")] = eq

    days = sorted(daily.keys())
    if len(days) >= 2:
        returns: List[float] = []
        prev_eq = starting_equity
        for day in days:
            eq = daily[day]
            if prev_eq > 0:
                returns.append((eq - prev_eq) / prev_eq)
            prev_eq = eq

        if len(returns) >= 2:
            avg_r = sum(returns) / len(returns)
            std = math.sqrt(sum((r - avg_r) ** 2 for r in returns) / (len(returns) - 1))
            downside = [r for r in returns if r < 0]
            down_dev = (
                math.sqrt(sum(r ** 2 for r in downside) / len(downside)) if downside else 0.0
            )
            # Volatility indices trade 24/7/365
            trading_days = 365
            ann_return = avg_r * trading_days
            ann_std = std * math.sqrt(trading_days)
            ann_down = down_dev * math.sqrt(trading_days)
            ps.sharpe_ratio = ann_return / ann_std if ann_std > 0 else 0.0
            ps.sortino_ratio = ann_return / ann_down if ann_down > 0 else 0.0

            lookback = returns[-min(len(returns), 252):]
            mean = sum(lookback) / len(lookback)
            sd = math.sqrt(sum((r - mean) ** 2 for r in lookback) / max(len(lookback) - 1, 1))
            ps.var_95 = round(mean + sd * _normal_inv_cdf(0.05), 4)
            ps.var_99 = round(mean + sd * _normal_inv_cdf(0.01), 4)

    # Max drawdown % with recovery tracking
    peak = starting_equity
    in_drawdown_since: Optional[datetime] = None
    max_dd_pct = 0.0
    max_recovery_days = 0

    for ts, eq in equity_curve:
        if eq >= peak:
            if in_drawdown_since is not None:
                recovery = (ts - in_drawdown_since).days
                if recovery > max_recovery_days:
                    max_recovery_days = recovery
            peak = eq
            in_drawdown_since = None
        else:
            dd_pct = (eq / peak - 1.0) * 100.0
            if dd_pct < max_dd_pct:
                max_dd_pct = dd_pct
                if in_drawdown_since is None:
                    in_drawdown_since = ts

    ps.max_drawdown_pct = round(max_dd_pct, 2)
    ps.drawdown_recovery_days = max_recovery_days

    return ps


# ---------------------------------------------------------------------------
# Per-symbol breakdown
# ---------------------------------------------------------------------------

def calculate_per_symbol_stats(trades: List[ClosedTrade]) -> List[SymbolStats]:
    by_symbol: Dict[str, List[ClosedTrade]] = {}
    for t in trades:
        by_symbol.setdefault(t.symbol, []).append(t)
    return [
        SymbolStats(symbol=sym, trade_stats=calculate_trade_stats(sym_trades))
        for sym, sym_trades in sorted(by_symbol.items())
    ]


# ---------------------------------------------------------------------------
# Streak risk assessment
# ---------------------------------------------------------------------------

def streak_risk_assessment(ts: TradeStats) -> dict:
    current = ts.current_consecutive_losses
    hist_max = ts.max_consecutive_losses
    threshold = max(3, round(hist_max * 0.75))
    return {
        "current_consecutive_losses": current,
        "max_historical_streak": hist_max,
        "pct_of_historical_max": round(current / hist_max * 100, 1) if hist_max > 0 else 0.0,
        "pause_threshold": threshold,
        "recommend_pause": current >= threshold,
    }


# ---------------------------------------------------------------------------
# Duration quality analysis
# ---------------------------------------------------------------------------

def duration_quality_report(trades: List[ClosedTrade]) -> dict:
    """Break down win rate by trade duration quartile to find the optimal hold window."""
    if not trades:
        return {}

    durations = sorted(t.duration.total_seconds() for t in trades)
    n = len(durations)
    q1 = durations[n // 4]
    q2 = durations[n // 2]
    q3 = durations[3 * n // 4]

    buckets = {
        f"0-{_fmt_duration(q1)}": [],
        f"{_fmt_duration(q1)}-{_fmt_duration(q2)}": [],
        f"{_fmt_duration(q2)}-{_fmt_duration(q3)}": [],
        f">{_fmt_duration(q3)}": [],
    }
    bucket_keys = list(buckets.keys())

    for trade in trades:
        d = trade.duration.total_seconds()
        if d <= q1:
            buckets[bucket_keys[0]].append(trade)
        elif d <= q2:
            buckets[bucket_keys[1]].append(trade)
        elif d <= q3:
            buckets[bucket_keys[2]].append(trade)
        else:
            buckets[bucket_keys[3]].append(trade)

    result = {}
    for label, bucket in buckets.items():
        if bucket:
            wins = sum(1 for t in bucket if t.is_win)
            result[label] = {
                "trades": len(bucket),
                "win_rate": round(wins / len(bucket) * 100, 1),
                "total_pnl": round(sum(t.profit_loss for t in bucket), 2),
            }

    return result
