#!/usr/bin/env python3
"""
Deriv auto-trader (multiplier / CFD-style) — no LLM in the loop.

Places MULTUP/MULTDOWN positions that HOLD open (floating P&L, visible pips)
and auto-close on stop-loss / take-profit — a MetaTrader-style model rather
than instant-settling binaries. Skips symbols that already have an open
position so positions don't stack. Runs as a Kubernetes CronJob; stdout is
captured in the Job logs.
"""
import os
import urllib.request
import urllib.error
import json
import sys
from datetime import datetime, timezone

BASE = os.getenv("TRADING_API_URL", "http://deriv-trading-bot:8000")
# Only symbols whose multiplier in LOWEST_MULT is actually accepted by Deriv.
# The bot's SYMBOL_MULTIPLIERS table is out of sync with Deriv for several
# symbols (R_10/R_25/R_75/1HZ10V/1HZ25V) — those are excluded until the table
# is fixed from Deriv's contracts_for.
SYMBOLS = ["R_50", "R_100", "1HZ100V"]
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "5.0"))   # stake must exceed STOP_LOSS
STOP_LOSS = float(os.getenv("STOP_LOSS", "2"))
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "4"))
BUY_THRESHOLD = int(os.getenv("BUY_THRESHOLD", "1"))
SELL_THRESHOLD = -BUY_THRESHOLD

# Lowest valid multiplier per symbol (from the bot's SYMBOL_MULTIPLIERS).
LOWEST_MULT = {"R_50": 80, "R_100": 40, "1HZ100V": 40}


def fetch(path, method="GET", body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"success": False, "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def fmt_score(s):
    return f"{s:+d}" if s is not None else "?"


now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
lines = [f"## Deriv Auto-Trade (multiplier) — {now}", ""]

# Streak/pause guard
pause_data = fetch("/api/portfolio/pause-status")
pause = pause_data.get("pause", {}) if pause_data.get("success") else {}
if pause.get("recommend_pause"):
    lines.append(f"⚠️  Streak pause active — {pause.get('reason', 'unknown')}")
    print("\n".join(lines)); sys.exit(0)

# Symbols that already hold an open position — skip to avoid stacking
open_resp = fetch("/api/trades/open")
held = {t.get("symbol") for t in open_resp.get("trades", [])} if open_resp.get("success") else set()
if held:
    lines.append(f"Holding open: {', '.join(sorted(held))}")

sig_data = fetch(f"/api/signals?symbols={','.join(SYMBOLS)}")
if not sig_data.get("success"):
    lines.append(f"❌ Signal fetch failed: {sig_data.get('error', 'unknown')}")
    print("\n".join(lines)); sys.exit(1)
signals = sig_data.get("signals", {})
errors = sig_data.get("errors", {})

lines.append("")
lines.append("| Symbol | Score | RSI | MACD | BB | Action |")
lines.append("|--------|-------|-----|------|----|--------|")

to_place = []
for sym in SYMBOLS:
    if sym not in signals:
        lines.append(f"| {sym} | — | — | — | — | ⚠ {errors.get(sym, 'no data')} |")
        continue
    s = signals[sym]
    score = s.get("composite_score", 0)
    rsi = s.get("rsi", {}); macd = s.get("macd", {}); bb = s.get("bb", {})
    rsi_str = f"{rsi.get('value', 0):.1f}" if rsi else "—"
    macd_str = "bull" if (macd.get("hist", 0) or 0) > 0 else "bear"
    bb_str = bb.get("position", "—") if bb else "—"

    if sym in held:
        action = "○ holding"
    elif score >= BUY_THRESHOLD:
        action = "🟢 BUY"; to_place.append((sym, "BUY", score))
    elif score <= SELL_THRESHOLD:
        action = "🔴 SELL"; to_place.append((sym, "SELL", score))
    else:
        action = "HOLD"
    lines.append(f"| {sym} | {fmt_score(score)} | {rsi_str} | {macd_str} | {bb_str} | {action} |")

lines.append("")
if not to_place:
    lines.append("**No new positions** — nothing crossed the threshold (or already holding).")
else:
    lines.append("**Positions Opened:**")
    for sym, direction, score in to_place:
        mult = LOWEST_MULT.get(sym, 0)
        result = fetch("/api/trade/multiplier", method="POST", body={
            "symbol": sym, "direction": direction, "amount": TRADE_AMOUNT,
            "multiplier": mult, "stop_loss": STOP_LOSS, "take_profit": TAKE_PROFIT,
        })
        data = str(result.get("data", ""))
        if result.get("success") and "placed" in data.lower():
            cid = data.split("#")[1].split(" ")[0] if "#" in data else "?"
            lines.append(f"- {sym}: **{direction}** ${TRADE_AMOUNT:.0f} @ {mult}x "
                         f"SL${STOP_LOSS:.0f}/TP${TAKE_PROFIT:.0f} — #{cid} (score {fmt_score(score)})")
        else:
            err = result.get("error") or data or str(result)
            lines.append(f"- {sym}: {direction} — ❌ {err[:120]}")

if errors:
    lines.append("")
    lines.append(f"**Skipped** (errors): {', '.join(errors.keys())}")

print("\n".join(lines))
