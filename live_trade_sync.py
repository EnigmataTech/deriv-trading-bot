"""Periodic sync of closed real-money trades from the kreation MT5 bridge into
Postgres, so the TUI's History tab has one durable, unified ledger instead of
depending on however long Deriv retains broker-side deal history — and so
trades closed by the bridge's own background time-stop worker (which never
touches Laravel's audit log) still end up recorded somewhere.

Currently-open live positions are NOT synced here — they stay real-time via
the TUI's Live tab, which queries the bridge directly. Only trades that have
actually closed get persisted, once, idempotently on mt5_ticket.
"""
import asyncio
import os
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from database import TradingRepository

logger = logging.getLogger(__name__)

BRIDGE_URL = os.getenv("MT5_BRIDGE_URL", "").rstrip("/")
BRIDGE_TOKEN = os.getenv("MT5_BRIDGE_TOKEN", "")
SYNC_INTERVAL = int(os.getenv("LIVE_TRADE_SYNC_INTERVAL", "180"))
HISTORY_DAYS = int(os.getenv("LIVE_TRADE_SYNC_DAYS", "30"))
LIVE_TRADE_USER_ID = os.getenv("LIVE_TRADE_USER_ID", "hermes_agent")


async def _fetch_deals() -> Optional[List[Dict[str, Any]]]:
    if not BRIDGE_URL or not BRIDGE_TOKEN:
        return None
    headers = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{BRIDGE_URL}/history",
            headers=headers,
            params={"days": HISTORY_DAYS},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            data = await r.json()
            return data if isinstance(data, list) else None


def _pair_closed_trades(deals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pair MT5 deal IN/OUT legs (linked by position_id) into one row per
    closed trade — same logic as the TUI's Live-history table."""
    by_position: Dict[Any, Dict[str, Any]] = {}
    for d in deals:
        pos_id = d.get("position_id")
        if pos_id is None:
            continue
        row = by_position.setdefault(pos_id, {})
        if d.get("entry") == 0:  # DEAL_ENTRY_IN
            row.update(
                open_price=d.get("price"), open_time=d.get("time"),
                dir="buy" if d.get("type") == 0 else "sell",
                volume=d.get("volume"), symbol=d.get("symbol"), ticket=pos_id,
            )
        elif d.get("entry") == 1:  # DEAL_ENTRY_OUT
            row["close_price"] = d.get("price")
            row["close_time"] = d.get("time")
            row["profit"] = (row.get("profit") or 0) + (d.get("profit") or 0)
    return [r for r in by_position.values() if "close_time" in r and "open_time" in r]


async def sync_live_trades() -> Dict[str, int]:
    """One sync pass. Returns {'seen': N, 'inserted': N} for logging."""
    deals = await _fetch_deals()
    if deals is None:
        return {"seen": 0, "inserted": 0}

    closed = _pair_closed_trades(deals)
    inserted = 0
    for t in closed:
        ticket = int(t["ticket"])
        try:
            already_have = TradingRepository.get_trade_by_trade_id(f"live-{ticket}")
            if already_have:
                continue
            TradingRepository.upsert_closed_live_trade(
                user_id=LIVE_TRADE_USER_ID,
                mt5_ticket=ticket,
                symbol=t["symbol"],
                trade_type=t["dir"],
                amount=float(t["volume"]),
                entry_price=float(t["open_price"]),
                exit_price=float(t["close_price"]),
                profit_loss=float(t.get("profit") or 0),
                created_at=datetime.fromtimestamp(t["open_time"], tz=timezone.utc),
                closed_at=datetime.fromtimestamp(t["close_time"], tz=timezone.utc),
            )
            inserted += 1
        except Exception as e:
            logger.warning("live_trade_sync: failed to upsert ticket %s: %s", ticket, e)

    return {"seen": len(closed), "inserted": inserted}


async def live_trade_sync_loop() -> None:
    """Background task — mirrors _balance_snapshot_loop's shape in main.py."""
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        try:
            result = await sync_live_trades()
            if result["inserted"]:
                logger.info(
                    "live_trade_sync: %d new live trade(s) recorded (%d seen)",
                    result["inserted"], result["seen"],
                )
        except Exception as e:
            logger.warning("live_trade_sync loop error: %s", e)
