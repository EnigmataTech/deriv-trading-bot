"""Historical validation for patterns.py's W/M (double-bottom/double-top)
detection — walk-forward, no lookahead, against real M15 OHLC.

Standalone analysis script. Does NOT place trades and is not imported by
main.py — the point is to see whether this has a real edge *before* it's
ever wired into the live signal path. Run manually:

    python backtest_patterns.py [SYMBOL] [--count N] [--order N] [--tolerance PCT]

Pulls candles from the running bot's REST API (TRADING_API_URL, same as
tui.py), so it sees exactly the OHLC data the live system would.
"""
import argparse
import os
from dataclasses import dataclass
from typing import List, Optional

import requests

from patterns import analyze_pattern, PatternResult

API_BASE = os.getenv("TRADING_API_URL", "http://localhost:8000").rstrip("/")


def fetch_candles(symbol: str, timeframe: str = "15m", count: int = 1000) -> List[dict]:
    r = requests.get(f"{API_BASE}/api/candles/{symbol}", params={"timeframe": timeframe, "count": count}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(data.get("error", "candle fetch failed"))
    candles = data["candles"]
    candles.sort(key=lambda c: c["time"])
    return candles


@dataclass
class SimTrade:
    pattern: str
    entry_index: int
    entry_price: float
    stop: float
    target: float
    exit_index: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None

    @property
    def pnl_pct(self) -> Optional[float]:
        if self.exit_price is None:
            return None
        direction = 1 if self.pattern == "W" else -1
        return direction * (self.exit_price - self.entry_price) / self.entry_price * 100


def run_backtest(candles: List[dict], order: int = 3, tolerance_pct: float = 0.5,
                  max_hold_bars: int = 96, stop_buffer_pct: float = 0.05) -> List[SimTrade]:
    opens = [c["open"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    n = len(candles)

    seen_pivots: set = set()  # (pattern, p1.index, p2.index) already traded
    trades: List[SimTrade] = []
    open_trade: Optional[SimTrade] = None

    min_bars = 2 * order + 10
    for i in range(min_bars, n):
        if open_trade is not None:
            # Manage the open trade using this bar's range before considering new entries.
            hi, lo = highs[i], closes[i]
            if open_trade.pattern == "W":
                if lows[i] <= open_trade.stop:
                    open_trade.exit_index, open_trade.exit_price, open_trade.exit_reason = i, open_trade.stop, "stop"
                    trades.append(open_trade); open_trade = None
                elif highs[i] >= open_trade.target:
                    open_trade.exit_index, open_trade.exit_price, open_trade.exit_reason = i, open_trade.target, "target"
                    trades.append(open_trade); open_trade = None
            else:  # "M"
                if highs[i] >= open_trade.stop:
                    open_trade.exit_index, open_trade.exit_price, open_trade.exit_reason = i, open_trade.stop, "stop"
                    trades.append(open_trade); open_trade = None
                elif lows[i] <= open_trade.target:
                    open_trade.exit_index, open_trade.exit_price, open_trade.exit_reason = i, open_trade.target, "target"
                    trades.append(open_trade); open_trade = None
            if open_trade is not None and (i - open_trade.entry_index) >= max_hold_bars:
                open_trade.exit_index, open_trade.exit_price, open_trade.exit_reason = i, closes[i], "time_stop"
                trades.append(open_trade); open_trade = None
            continue  # one position at a time, same discipline as the live cron job

        result: PatternResult = analyze_pattern(highs[: i + 1], lows[: i + 1], closes[i], order, tolerance_pct)
        if not result.pattern or not result.confirmed:
            continue
        key = (result.pattern, result.p1.index, result.p2.index)
        if key in seen_pivots:
            continue
        seen_pivots.add(key)

        entry_price = opens[i + 1] if i + 1 < n else closes[i]
        entry_index = i + 1 if i + 1 < n else i
        buffer = result.neckline * stop_buffer_pct / 100
        if result.pattern == "W":
            stop = min(result.p1.price, result.p2.price) - buffer
        else:
            stop = max(result.p1.price, result.p2.price) + buffer
        open_trade = SimTrade(
            pattern=result.pattern, entry_index=entry_index, entry_price=entry_price,
            stop=stop, target=result.target,
        )

    return trades


def summarize(trades: List[SimTrade]) -> None:
    closed = [t for t in trades if t.exit_price is not None]
    if not closed:
        print("No completed pattern trades in this window.")
        return
    wins = [t for t in closed if (t.pnl_pct or 0) > 0]
    losses = [t for t in closed if (t.pnl_pct or 0) <= 0]
    gross_win = sum(t.pnl_pct for t in wins)
    gross_loss = sum(t.pnl_pct for t in losses)
    print(f"Trades: {len(closed)}  (W-pattern: {sum(1 for t in closed if t.pattern=='W')}, "
          f"M-pattern: {sum(1 for t in closed if t.pattern=='M')})")
    print(f"Wins: {len(wins)}  Losses: {len(losses)}  Win rate: {100*len(wins)/len(closed):.1f}%")
    print(f"Gross win: {gross_win:+.2f}%  Gross loss: {gross_loss:+.2f}%  "
          f"Net: {gross_win+gross_loss:+.2f}%  "
          f"PF: {(gross_win/abs(gross_loss)) if gross_loss else float('inf'):.2f}")
    print()
    for t in closed:
        print(f"  {t.pattern}  bar {t.entry_index:>4} @ {t.entry_price:>10.2f}  -> "
              f"bar {t.exit_index:>4} @ {t.exit_price:>10.2f}  "
              f"({t.exit_reason:<9})  {t.pnl_pct:+.2f}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", nargs="?", default="R_75")
    ap.add_argument("--timeframe", default="15m")
    ap.add_argument("--count", type=int, default=1000)
    ap.add_argument("--order", type=int, default=3)
    ap.add_argument("--tolerance", type=float, default=0.5)
    ap.add_argument("--max-hold-bars", type=int, default=96)
    args = ap.parse_args()

    candles = fetch_candles(args.symbol, args.timeframe, args.count)
    print(f"Loaded {len(candles)} {args.timeframe} candles for {args.symbol} "
          f"({candles[0]['time']} .. {candles[-1]['time']})")
    print(f"order={args.order}  tolerance={args.tolerance}%  max_hold_bars={args.max_hold_bars}\n")

    trades = run_backtest(candles, args.order, args.tolerance, args.max_hold_bars)
    summarize(trades)
