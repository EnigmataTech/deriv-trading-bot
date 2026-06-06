#!/usr/bin/env python3
"""
Deriv fast auto-trader — no LLM in the loop.
Fetches batch signals, trades on score >= +/-2 with one confirmation check.

Runs as a Kubernetes CronJob inside the cluster (24/7, independent of any
workstation). Hits the bot's in-cluster Service directly. stdout is captured
in the Job logs (kubectl logs job/...).
"""
import os
import urllib.request
import urllib.error
import json
import sys
from datetime import datetime, timezone

# In-cluster Service URL (same namespace). Override with TRADING_API_URL if needed.
BASE = os.getenv("TRADING_API_URL", "http://deriv-trading-bot:8000")
SYMBOLS = ["R_10", "R_25", "R_50", "R_75", "R_100", "1HZ10V", "1HZ25V", "1HZ100V"]
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "1.0"))
TRADE_DURATION = int(os.getenv("TRADE_DURATION", "5"))
TRADE_DURATION_UNIT = os.getenv("TRADE_DURATION_UNIT", "t")  # ticks — seconds<15 are rejected
BUY_THRESHOLD = int(os.getenv("BUY_THRESHOLD", "2"))
SELL_THRESHOLD = -BUY_THRESHOLD


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


def fmt_score(score):
    return f"{score:+d}" if score is not None else "?"


now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
lines = [f"## Deriv Auto-Trade — {now}", ""]

# Check streak/pause status
pause_data = fetch("/api/portfolio/pause-status")
pause = pause_data.get("pause", {}) if pause_data.get("success") else {}
if pause.get("recommend_pause"):
    lines.append(f"⚠️  Streak pause active — {pause.get('reason', 'unknown reason')}")
    lines.append("No trades placed.")
    print("\n".join(lines))
    sys.exit(0)

# Fetch all signals in one batch call
symbols_param = ",".join(SYMBOLS)
sig_data = fetch(f"/api/signals?symbols={symbols_param}")
if not sig_data.get("success"):
    lines.append(f"❌ Signal fetch failed: {sig_data.get('error', 'unknown')}")
    print("\n".join(lines))
    sys.exit(1)

signals = sig_data.get("signals", {})
errors = sig_data.get("errors", {})

lines.append("**Signal Scan:**")
lines.append("| Symbol | Score | RSI | MACD | BB | Call |")
lines.append("|--------|-------|-----|------|----|------|")

trades_to_place = []
for sym in SYMBOLS:
    if sym not in signals:
        lines.append(f"| {sym} | — | — | — | — | ⚠ {errors.get(sym, 'no data')} |")
        continue
    sig = signals[sym]
    score = sig.get("composite_score", 0)
    call = sig.get("call", "HOLD")
    rsi_v = sig.get("rsi", {})
    macd_v = sig.get("macd", {})
    bb_v = sig.get("bb", {})
    rsi_str = f"{rsi_v.get('value', 0):.1f}" if rsi_v else "—"
    macd_str = "bull" if (macd_v.get("hist", 0) or 0) > 0 else "bear"
    bb_str = bb_v.get("position", "—") if bb_v else "—"

    action, emoji = "HOLD", ""
    if score >= BUY_THRESHOLD and call == "BUY":
        action, emoji = "CALL ✓", "🟢"
        trades_to_place.append({"symbol": sym, "direction": "CALL", "score": score})
    elif score <= SELL_THRESHOLD and call == "SELL":
        action, emoji = "PUT ✓", "🔴"
        trades_to_place.append({"symbol": sym, "direction": "PUT", "score": score})

    lines.append(f"| {sym} | {fmt_score(score)} | {rsi_str} | {macd_str} | {bb_str} | {emoji}{action} |")

lines.append("")

if not trades_to_place:
    lines.append(f"**No trades placed** — no symbol crossed the ±{BUY_THRESHOLD} threshold.")
else:
    lines.append("**Trades Placed:**")
    for t in trades_to_place:
        result = fetch("/api/trade", method="POST", body={
            "symbol": t["symbol"],
            "amount": TRADE_AMOUNT,
            "direction": t["direction"],
            "duration": TRADE_DURATION,
            "duration_unit": TRADE_DURATION_UNIT,
        })
        data = str(result.get("data", ""))
        # The REST endpoint returns success=True even when Deriv rejects the
        # order (the error text comes back in `data`), so confirm the wording.
        placed = result.get("success") and "successfully" in data.lower()
        if placed:
            cid = "?"
            for ln in data.splitlines():
                if "Contract ID:" in ln:
                    cid = ln.split("Contract ID:")[1].strip()
            lines.append(f"- {t['symbol']}: **{t['direction']}** ${TRADE_AMOUNT:.2f} "
                         f"×{TRADE_DURATION}{TRADE_DURATION_UNIT} — contract `{cid}` "
                         f"(score {fmt_score(t['score'])})")
        else:
            err = data or result.get("error") or result.get("message") or str(result)
            lines.append(f"- {t['symbol']}: {t['direction']} — ❌ failed: {err}")

if errors:
    lines.append("")
    lines.append(f"**Skipped** (errors): {', '.join(errors.keys())}")

print("\n".join(lines))
