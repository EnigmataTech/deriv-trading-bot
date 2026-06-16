#!/usr/bin/env python3
"""Weekly strategy-performance report.

Reads closed trades from the trading DB, computes performance cuts (overall +
trailing window, per-symbol, per-strategy), sends a compact summary to Telegram,
and records the full report to the `strategy_reports` table and a dated markdown
file under reports/.

Run manually:
    cd ~/workspace/deriv-trading-bot && uv run python scripts/weekly_report.py
Scheduled weekly via the weekly-report.timer systemd user unit on the VPS.

Env: DATABASE_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (same as the bot).
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import SessionLocal, Trade, StrategyReport  # noqa: E402
from notify import notify_telegram  # noqa: E402

WINDOW_DAYS = int(os.getenv("REPORT_WINDOW_DAYS", "7"))
# When the refined ruleset went live — trades after this are "post-refinement".
REFINED_SINCE = "2026-06-16"


def _stats(pnls):
    """(n, win_rate 0..1, total, profit_factor, expectancy) for a list of P&L."""
    n = len(pnls)
    if n == 0:
        return 0, 0.0, 0.0, 0.0, 0.0
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = sum(pnls)
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    return n, len(wins) / n, total, pf, total / n


def _strategy_of(reason):
    r = (reason or "").lower()
    if "spike" in r:
        return "spike (Crash/Boom)"
    for s in ("+3", "+2", "-2", "-3"):
        if f"score {s}" in r:
            return f"score {s}"
    if r.startswith("auto"):
        return "auto other"
    return "manual/test"


def _section(title, rows):
    """rows: list of (label, pnls-list). Returns formatted lines."""
    out = [title]
    for label, pnls in rows:
        n, wr, tot, pf, exp = _stats(pnls)
        if n == 0:
            continue
        out.append(f"  {label:<22} {n:>3} | win {wr*100:4.0f}% | "
                   f"P&L ${tot:+7.2f} | PF {pf:4.2f} | exp ${exp:+.2f}")
    return out


def build_report():
    db = SessionLocal()
    try:
        closed = db.query(Trade).filter(
            Trade.status == "closed", Trade.profit_loss.isnot(None)).all()
    finally:
        db.close()

    now = datetime.utcnow()
    window_start = now - timedelta(days=WINDOW_DAYS)
    recent = [t for t in closed if t.created_at and t.created_at >= window_start]

    all_pnls = [float(t.profit_loss) for t in closed]
    rec_pnls = [float(t.profit_loss) for t in recent]

    # Per-symbol and per-strategy over the trailing window
    by_symbol, by_strat = {}, {}
    for t in recent:
        by_symbol.setdefault(t.symbol, []).append(float(t.profit_loss))
        by_strat.setdefault(_strategy_of(t.reason), []).append(float(t.profit_loss))
    sym_rows = sorted(by_symbol.items(), key=lambda kv: sum(kv[1]))
    strat_rows = sorted(by_strat.items(), key=lambda kv: sum(kv[1]))

    lines = [f"📊 Weekly Strategy Report — {now:%Y-%m-%d}",
             f"(refined ruleset live since {REFINED_SINCE}; longs-only, SL/TP, time-stop)",
             ""]
    n, wr, tot, pf, exp = _stats(rec_pnls)
    lines.append(f"Last {WINDOW_DAYS}d: {n} trades | win {wr*100:.0f}% | "
                 f"P&L ${tot:+.2f} | PF {pf:.2f} | exp ${exp:+.2f}")
    an, awr, atot, apf, _ = _stats(all_pnls)
    lines.append(f"All-time: {an} trades | win {awr*100:.0f}% | "
                 f"P&L ${atot:+.2f} | PF {apf:.2f}")
    lines.append("")
    lines += _section(f"By symbol (last {WINDOW_DAYS}d):", sym_rows)
    lines.append("")
    lines += _section(f"By strategy (last {WINDOW_DAYS}d):", strat_rows)
    lines.append("")
    # Go-live readiness gate
    if n >= 50 and pf > 1.5:
        lines.append("✅ Go-live gate: MET (≥50 trades, PF > 1.5). Consider live.")
    else:
        need = []
        if n < 50:
            need.append(f"{50-n} more trades")
        if pf <= 1.5:
            need.append(f"PF > 1.5 (now {pf:.2f})")
        lines.append(f"⏳ Go-live gate: not yet — need {', '.join(need)}.")

    return "\n".join(lines), {
        "period_days": WINDOW_DAYS, "total_trades": n,
        "win_rate": wr, "total_pnl": tot, "profit_factor": pf,
    }


def record(report_text, metrics):
    # 1) durable DB row
    db = SessionLocal()
    try:
        db.add(StrategyReport(report_text=report_text, **metrics))
        db.commit()
    except Exception as e:
        print(f"DB record failed: {e}", file=sys.stderr)
        db.rollback()
    finally:
        db.close()
    # 2) dated markdown file
    try:
        rdir = os.path.join(os.path.dirname(__file__), "..", "reports")
        os.makedirs(rdir, exist_ok=True)
        path = os.path.join(rdir, f"strategy-{datetime.utcnow():%Y-%m-%d}.md")
        with open(path, "w") as f:
            f.write(report_text + "\n")
    except Exception as e:
        print(f"File record failed: {e}", file=sys.stderr)


def main():
    report_text, metrics = build_report()
    print(report_text)
    notify_telegram(report_text)
    record(report_text, metrics)


if __name__ == "__main__":
    main()
