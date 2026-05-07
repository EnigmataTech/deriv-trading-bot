"""
Smoke test for the new analytics MCP tools using an in-memory SQLite DB.
No Postgres / no live Deriv connection / no FastMCP server needed.

Patches DATABASE_URL before importing main.py, populates Trade + Portfolio rows,
then invokes each of the 4 new tools + the 2 upgraded tools and checks output.

Run: python test_mcp_tools.py
"""

import os
import sys
import traceback
from datetime import datetime, timedelta

# --- patch BEFORE importing main / database ---
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["MCP_AGENT_USER_ID"] = "smoke_user"
os.environ["AUTH_ENABLED"] = "false"
os.environ.setdefault("DERIV_API_TOKEN", "smoke")
os.environ.setdefault("DERIV_APP_ID", "1089")

import database  # noqa: E402
from database import Trade, Portfolio, Base, engine, SessionLocal  # noqa: E402

# Create tables on the in-memory DB
Base.metadata.create_all(engine)


PASS = 0
FAIL = 0
FAILS: list = []


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILS.append(f"{name} ({detail})")
        print(f"  FAIL  {name}  -- {detail}")


def seed_db():
    """Populate the in-memory DB with a known set of closed + open trades."""
    db = SessionLocal()
    try:
        # 6 closed trades across 2 symbols, with deterministic P&L and durations
        base = datetime(2026, 5, 1, 10, 0, 0)
        rows = [
            # trade_id, symbol, ttype, amount, entry, exit, pnl, status, opened_offset_h, dur_min
            ("c1", "R_100", "buy",  10.0, 100.0, 110.0,  10.0, "closed", 0,   30),
            ("c2", "R_100", "buy",  10.0, 110.0, 105.0,  -5.0, "closed", 24,  45),
            ("c3", "R_100", "buy",  10.0, 105.0, 120.0,  15.0, "closed", 48,  60),
            ("c4", "R_100", "buy",  10.0, 120.0, 112.0,  -8.0, "closed", 72,  20),
            ("c5", "R_50",  "buy",  10.0, 200.0, 205.0,   5.0, "closed", 96,  90),
            ("c6", "R_50",  "buy",  10.0, 205.0, 202.0,  -3.0, "closed", 120, 15),
            # 1 open trade — should be excluded by analytics
            ("o1", "R_100", "buy",  10.0, 112.0, None,   None, "open",   144,  0),
        ]
        for tid, sym, tt, amt, ep, xp, pnl, st, oh, dm in rows:
            opened = base + timedelta(hours=oh)
            closed_at = (opened + timedelta(minutes=dm)) if st == "closed" else None
            db.add(Trade(
                user_id="smoke_user",
                trade_id=tid,
                symbol=sym,
                trade_type=tt,
                amount=amt,
                entry_price=ep,
                exit_price=xp,
                profit_loss=pnl,
                status=st,
                created_at=opened,
                closed_at=closed_at,
            ))

        # Portfolio row — total P&L = 14, so starting equity should be balance-pnl = 986
        db.add(Portfolio(
            user_id="smoke_user",
            balance=1014.0,
            equity=1014.0,
            margin=0.0,
            free_margin=1014.0,
        ))
        db.commit()
    finally:
        db.close()


def run_smoke():
    print("\n[seeding in-memory DB]")
    seed_db()

    # Import main now that DB is set up. Some import-time side-effects exist
    # (logger, JWT setup, FastMCP instance) — they should all succeed offline.
    print("\n[importing main.py]")
    import main  # noqa: F401
    print("  PASS  main imported without crash")
    global PASS
    PASS += 1

    # ---- analyze_portfolio_performance ----
    print("\n[analyze_portfolio_performance]")
    out = main.analyze_portfolio_performance()
    print("  --- output ---")
    for line in out.splitlines():
        print("  | " + line)
    print("  --- end ---")
    check("contains header", "Portfolio Performance Analysis" in out)
    check("reports 6 closed trades", "6 closed" in out, out[:200])
    check("reports 1 open", "1 open" in out)
    check("Sharpe printed", "Sharpe (trade)" in out)
    check("Sortino printed", "Sortino (trade)" in out)
    check("Profit Factor printed", "Profit Factor:" in out)
    check("Expectancy printed", "Expectancy:" in out)
    check("Max Closed DD printed", "Max Closed DD:" in out)

    # ---- get_risk_metrics ----
    print("\n[get_risk_metrics]")
    out = main.get_risk_metrics()
    print("  --- output ---")
    for line in out.splitlines():
        print("  | " + line)
    print("  --- end ---")
    check("contains header", "Risk Metrics" in out)
    check("Start Equity rendered", "Start Equity:" in out)
    check("CAGR rendered", "CAGR:" in out)
    check("Max Drawdown rendered", "Max Drawdown:" in out)
    check("VaR 95 rendered", "VaR 95%" in out)
    check("VaR 99 rendered", "VaR 99%" in out)

    # ---- get_per_symbol_performance ----
    print("\n[get_per_symbol_performance]")
    out = main.get_per_symbol_performance()
    print("  --- output ---")
    for line in out.splitlines():
        print("  | " + line)
    print("  --- end ---")
    check("R_100 listed", "[R_100]" in out)
    check("R_50 listed", "[R_50]" in out)
    # R_100 has higher P&L than R_50 (12 vs 2) → should appear first
    r100_pos = out.find("[R_100]")
    r50_pos = out.find("[R_50]")
    check("R_100 sorted before R_50", 0 <= r100_pos < r50_pos,
          f"r100={r100_pos} r50={r50_pos}")

    # ---- analyze_trade_quality ----
    print("\n[analyze_trade_quality]")
    out = main.analyze_trade_quality()
    print("  --- output ---")
    for line in out.splitlines():
        print("  | " + line)
    print("  --- end ---")
    check("contains header", "Trade Quality Analysis" in out)
    check("avg winning duration", "Avg winning trade duration" in out)
    check("best bucket reported", "Best duration bucket" in out)
    check("at least one quartile bucket shown", "WR " in out)

    # ---- analyze_streak_risk ----
    print("\n[analyze_streak_risk]")
    out = main.analyze_streak_risk()
    print("  --- output ---")
    for line in out.splitlines():
        print("  | " + line)
    print("  --- end ---")
    check("header present", "Streak Risk Assessment" in out)
    check("recommendation line present", "RECOMMEND PAUSE" in out)
    check("per-symbol section present", "Per-symbol streak status" in out)
    check("R_100 listed in streak", "R_100" in out)
    check("R_50 listed in streak", "R_50" in out)

    # ---- empty-state check: drop closed trades, retry ----
    print("\n[empty-state guard for analytics tools]")
    db = SessionLocal()
    try:
        db.query(Trade).filter(Trade.status == "closed").delete()
        db.commit()
    finally:
        db.close()

    out_empty = main.analyze_portfolio_performance()
    check("portfolio empty branch", "No closed trades" in out_empty,
          out_empty[:120])
    check("get_risk_metrics empty branch",
          "No closed trades" in main.get_risk_metrics())
    check("get_per_symbol_performance empty branch",
          "No closed trades" in main.get_per_symbol_performance())
    check("analyze_trade_quality empty branch",
          "No closed trades" in main.analyze_trade_quality())
    check("analyze_streak_risk empty branch",
          "No closed trades" in main.analyze_streak_risk())

    # ---- calculate_technical_indicators (stubbed Deriv client) ----
    print("\n[calculate_technical_indicators — stubbed Deriv client]")

    class StubDerivClient:
        """Minimal stub matching DerivAPIClient.get_ticks_history shape."""
        def __init__(self, prices):
            self._prices = prices
            self.last_request = None

        async def get_ticks_history(self, symbol: str, count: int = 100):
            self.last_request = (symbol, count)
            # Return a window of `count` ticks (or all if fewer)
            slice_ = self._prices[-count:] if len(self._prices) >= count else self._prices
            return {"history": {"prices": slice_, "times": list(range(len(slice_)))}}

    # Build a deterministic tick sequence: noisy ramp from 100 → ~115 over 200 ticks.
    # Step is +0.075 with a small alternating wobble so RSI isn't pegged at 100.
    prices = []
    p = 100.0
    for i in range(200):
        wobble = 0.05 if i % 2 == 0 else -0.03
        p += 0.075 + wobble
        prices.append(round(p, 5))

    stub = StubDerivClient(prices)

    async def _fake_get_client():
        return stub
    real_getter = main.get_deriv_client
    main.get_deriv_client = _fake_get_client

    try:
        # SMA
        out = main.calculate_technical_indicators("R_100", "sma", 14)
        print(f"  | sma -> {out}")
        check("SMA returns single value", out.startswith("SMA(14) for R_100:"))
        check("SMA passes period*3 count", stub.last_request[1] == max(100, 14 * 3))

        # EMA
        out = main.calculate_technical_indicators("R_100", "ema", 14)
        print(f"  | ema -> {out}")
        check("EMA returns single value", out.startswith("EMA(14) for R_100:"))

        # RSI with interpretation tag
        out = main.calculate_technical_indicators("R_100", "rsi", 14)
        print(f"  | rsi -> {out}")
        check("RSI returns value", out.startswith("RSI(14) for R_100:"))
        check("RSI tags interpretation",
              any(tag in out for tag in ["overbought", "oversold", "neutral"]),
              out)

        # MACD multi-line
        out = main.calculate_technical_indicators("R_100", "macd", 14)
        print("  | macd ->")
        for ln in out.splitlines():
            print("  |   " + ln)
        check("MACD header", out.startswith("MACD for R_100:"))
        check("MACD has line/signal/hist",
              "MACD line:" in out and "Signal:" in out and "Histogram:" in out)
        check("MACD includes cross label",
              any(t in out for t in ["bullish cross", "bearish cross", "neutral"]))

        # Bollinger Bands with default period=14 → tool maps to 20
        out = main.calculate_technical_indicators("R_100", "bb", 14)
        print("  | bb ->")
        for ln in out.splitlines():
            print("  |   " + ln)
        check("BB uses 20 when period=14", "Bollinger Bands(20" in out)
        check("BB shows position label",
              any(t in out for t in ["above upper band", "below lower band", "within bands"]))

        # ATR
        out = main.calculate_technical_indicators("R_100", "atr", 14)
        print(f"  | atr -> {out}")
        check("ATR returns value", out.startswith("ATR(14) for R_100:"))
        check("ATR tagline included", "tick-to-tick range" in out)

        # Unknown indicator → error string
        out = main.calculate_technical_indicators("R_100", "stoch", 14)
        check("unknown indicator → fallback", "Unsupported indicator" in out, out)

        # Insufficient ticks → guard message
        small_stub = StubDerivClient([100.0, 101.0, 102.0])
        async def _small():
            return small_stub
        main.get_deriv_client = _small
        out = main.calculate_technical_indicators("R_100", "sma", 50)
        check("SMA insufficient-ticks guard",
              out == "Need at least 50 ticks for SMA(50)." or "Need at least" in out,
              out)
    finally:
        main.get_deriv_client = real_getter


def main_entry():
    try:
        run_smoke()
    except Exception as e:
        global FAIL
        FAIL += 1
        FAILS.append(f"smoke crashed: {e}")
        print("\nCRASH:")
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
    main_entry()
