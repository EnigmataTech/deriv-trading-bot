"""
MT5 broker client — talks to a headless MetaTrader 5 terminal on the VPS via the
`mt5linux` rpyc bridge (see mt5-bridge.service on stinger, 127.0.0.1:18812).

Drop-in replacement for the read paths of DerivAPIClient: the read methods
(`get_account_balance`, `get_candles`, `get_ticks_history`) return the SAME
Deriv-style shapes main.py already consumes. Trading methods are MT5-native
(lot size + price-level SL/TP), wired into the REST endpoints in Phase 3.

Symbols are identified by their MT5 names directly (e.g. "Volatility 75 Index").
"""
import os
import asyncio
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

MT5_HOST = os.getenv("MT5_HOST", "127.0.0.1")
MT5_PORT = int(os.getenv("MT5_PORT", "18812"))
MT5_TERMINAL_PATH = os.getenv(
    "MT5_TERMINAL_PATH",
    r"C:\Program Files\MetaTrader 5 Terminal\terminal64.exe",
)
MT5_CREDS_FILE = os.getenv("MT5_CREDS_FILE", os.path.expanduser("~/.mt5creds"))

# Canonical symbols = MT5 names. Default low-leverage synthetics the bot trades.
MT5_SYMBOLS = [
    "Volatility 10 Index", "Volatility 25 Index", "Volatility 50 Index",
    "Volatility 75 Index", "Volatility 100 Index",
    "Volatility 10 (1s) Index", "Volatility 25 (1s) Index",
    "Volatility 50 (1s) Index", "Volatility 75 (1s) Index",
    "Volatility 100 (1s) Index",
]


def _load_creds() -> Dict[str, str]:
    creds = {}
    try:
        for line in open(MT5_CREDS_FILE):
            k, _, v = line.strip().partition("=")
            if k:
                creds[k] = v
    except FileNotFoundError:
        pass
    # env overrides
    for k in ("LOGIN", "PASSWORD", "SERVER"):
        if os.getenv(f"MT5_{k}"):
            creds[k] = os.getenv(f"MT5_{k}")
    return creds


class MT5Client:
    """Async wrapper over the synchronous mt5linux rpyc proxy.

    All proxy calls are serialized with a lock and run in a thread so they don't
    block the event loop. Reconnects/re-initializes on demand.
    """

    def __init__(self, host: str = MT5_HOST, port: int = MT5_PORT):
        self.host = host
        self.port = port
        self._mt5 = None            # mt5linux MetaTrader5 proxy
        self._tf_map: Dict[int, Any] = {}   # granularity seconds -> mt5 TIMEFRAME_*
        self._lock = asyncio.Lock()

    # ---- connection -----------------------------------------------------
    def _connect_sync(self) -> bool:
        from mt5linux import MetaTrader5
        mt5 = MetaTrader5(host=self.host, port=self.port)
        # Attach to the bridge-launched terminal (already logged in + algo-enabled
        # via mt5-startup.ini). Fall back to launching it ourselves if absent.
        ok = mt5.initialize()
        if not ok:
            creds = _load_creds()
            ok = mt5.initialize(
                path=MT5_TERMINAL_PATH,
                login=int(creds["LOGIN"]),
                password=creds["PASSWORD"],
                server=creds["SERVER"],
                timeout=120000,
            )
        if not ok:
            logger.error("MT5 initialize failed: %s", mt5.last_error())
            return False
        self._mt5 = mt5
        self._tf_map = {
            60: mt5.TIMEFRAME_M1, 300: mt5.TIMEFRAME_M5,
            900: mt5.TIMEFRAME_M15, 1800: mt5.TIMEFRAME_M30,
            3600: mt5.TIMEFRAME_H1, 14400: mt5.TIMEFRAME_H4,
            86400: mt5.TIMEFRAME_D1,
        }
        return True

    def _ensure_sync(self):
        if self._mt5 is None:
            if not self._connect_sync():
                raise RuntimeError("MT5 not connected")
        return self._mt5

    async def _call(self, fn, *args, **kwargs):
        """Serialize + run a sync function that receives the live mt5 proxy."""
        async with self._lock:
            return await asyncio.to_thread(lambda: fn(self._ensure_sync(), *args, **kwargs))

    async def connect(self) -> bool:
        try:
            async with self._lock:
                return await asyncio.to_thread(self._connect_sync)
        except Exception as e:
            logger.error("MT5 connect error: %s", e)
            return False

    async def disconnect(self):
        if self._mt5 is not None:
            try:
                await asyncio.to_thread(self._mt5.shutdown)
            except Exception:
                pass
            self._mt5 = None

    # ---- helpers --------------------------------------------------------
    @staticmethod
    def _select(mt5, symbol: str):
        mt5.symbol_select(symbol, True)

    @staticmethod
    def _filling(mt5, symbol: str):
        """Pick a supported order filling mode from the symbol's flags
        (Deriv synthetics require FOK, not IOC). SYMBOL_FILLING_FOK=1, IOC=2."""
        si = mt5.symbol_info(symbol)
        fm = getattr(si, "filling_mode", 0) if si else 0
        if fm & 1:
            return mt5.ORDER_FILLING_FOK
        if fm & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    # ---- read paths (Deriv-compatible shapes) ---------------------------
    async def get_account_balance(self) -> Dict[str, Any]:
        def _f(mt5):
            ai = mt5.account_info()
            if ai is None:
                return {"error": {"message": "account_info unavailable"}}
            return {"balance": {
                "balance": float(ai.balance),
                "currency": str(ai.currency),
                "equity": float(ai.equity),
                "margin": float(ai.margin),
                "free_margin": float(ai.margin_free),
            }}
        try:
            return await self._call(_f)
        except Exception as e:
            return {"error": {"message": str(e)}}

    async def get_candles(self, symbol: str, granularity: int = 60, count: int = 50) -> Dict[str, Any]:
        def _f(mt5):
            self._select(mt5, symbol)
            tf = self._tf_map.get(granularity, mt5.TIMEFRAME_M1)
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
            if rates is None:
                return {"error": {"message": f"no rates for {symbol}: {mt5.last_error()}"}}
            candles = [{
                "epoch": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]),
            } for r in rates]
            return {"candles": candles}
        try:
            return await self._call(_f)
        except Exception as e:
            return {"error": {"message": str(e)}}

    async def get_ticks_history(self, symbol: str, count: int = 100) -> Dict[str, Any]:
        """Price series for signal computation. Uses M1 closes (robust; the
        indicators only need a clean recent series)."""
        def _f(mt5):
            self._select(mt5, symbol)
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, count)
            if rates is None:
                return {"error": {"message": f"no history for {symbol}"}}
            return {"history": {
                "prices": [float(r[4]) for r in rates],
                "times": [int(r[0]) for r in rates],
            }}
        try:
            return await self._call(_f)
        except Exception as e:
            return {"error": {"message": str(e)}}

    async def get_tick(self, symbol: str) -> Dict[str, Any]:
        def _f(mt5):
            self._select(mt5, symbol)
            t = mt5.symbol_info_tick(symbol)
            if t is None:
                return {"error": {"message": f"no tick for {symbol}"}}
            return {"bid": float(t.bid), "ask": float(t.ask),
                    "quote": float(t.bid), "epoch": int(t.time)}
        try:
            return await self._call(_f)
        except Exception as e:
            return {"error": {"message": str(e)}}

    async def get_symbol_specs(self, symbol: str) -> Dict[str, Any]:
        """Volume constraints for a symbol so callers can size lots correctly
        (synthetics vary: Vol75=0.01, Vol75(1s)=0.05, Vol100=1.0)."""
        def _f(mt5):
            self._select(mt5, symbol)
            si = mt5.symbol_info(symbol)
            if si is None:
                return {"error": {"message": f"no symbol_info for {symbol}"}}
            return {
                "symbol": symbol,
                "volume_min": float(getattr(si, "volume_min", 0.0)),
                "volume_max": float(getattr(si, "volume_max", 0.0)),
                "volume_step": float(getattr(si, "volume_step", 0.0)),
                "digits": int(getattr(si, "digits", 2)),
                "point": float(getattr(si, "point", 0.0)),
                "stops_level": int(getattr(si, "trade_stops_level", 0)),
                "trade_allowed": getattr(si, "trade_mode", 0) != 0,
                # Monetary value of one tick per 1.0 lot, and the tick size in
                # price units — used for fixed-fractional (2%) position sizing.
                "tick_value": float(getattr(si, "trade_tick_value", 0.0)),
                "tick_size": float(getattr(si, "trade_tick_size", 0.0)),
                "contract_size": float(getattr(si, "trade_contract_size", 0.0)),
            }
        try:
            return await self._call(_f)
        except Exception as e:
            return {"error": {"message": str(e)}}

    async def get_ticks(self, symbol: str) -> Dict[str, Any]:
        """Latest tick in the Deriv-compatible shape callers expect:
        {"tick": {"quote", "epoch", "symbol"}}. Uses the live bid."""
        def _f(mt5):
            self._select(mt5, symbol)
            t = mt5.symbol_info_tick(symbol)
            if t is None:
                return {"error": {"message": f"no tick for {symbol}"}}
            return {"tick": {"quote": float(t.bid), "epoch": int(t.time), "symbol": symbol}}
        try:
            return await self._call(_f)
        except Exception as e:
            return {"error": {"message": str(e)}}

    async def get_symbol_spec(self, symbol: str) -> Dict[str, Any]:
        def _f(mt5):
            si = mt5.symbol_info(symbol)
            if si is None:
                return {"error": {"message": f"unknown symbol {symbol}"}}
            return {"symbol": symbol, "volume_min": float(si.volume_min),
                    "volume_step": float(si.volume_step), "volume_max": float(si.volume_max),
                    "digits": int(si.digits), "point": float(si.point)}
        return await self._call(_f)

    # ---- trading (MT5-native: lot + price-level SL/TP) ------------------
    async def place_order(self, symbol: str, side: str, lot: float,
                          sl_price: Optional[float] = None, tp_price: Optional[float] = None,
                          entry_price: Optional[float] = None, deviation: int = 20,
                          comment: str = "hermes") -> Dict[str, Any]:
        """side: 'buy'|'sell'. Market order, or pending if entry_price given."""
        def _f(mt5):
            self._select(mt5, symbol)
            t = mt5.symbol_info_tick(symbol)
            buy = side.lower() == "buy"
            req = {
                "symbol": symbol, "volume": float(lot),
                "deviation": deviation, "magic": 778899, "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling(mt5, symbol),
            }
            if entry_price is None:
                req["action"] = mt5.TRADE_ACTION_DEAL
                req["type"] = mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL
                req["price"] = float(t.ask if buy else t.bid)
            else:
                req["action"] = mt5.TRADE_ACTION_PENDING
                cur = float(t.ask if buy else t.bid)
                if buy:
                    req["type"] = mt5.ORDER_TYPE_BUY_LIMIT if entry_price < cur else mt5.ORDER_TYPE_BUY_STOP
                else:
                    req["type"] = mt5.ORDER_TYPE_SELL_LIMIT if entry_price > cur else mt5.ORDER_TYPE_SELL_STOP
                req["price"] = float(entry_price)
            if sl_price is not None:
                req["sl"] = float(sl_price)
            if tp_price is not None:
                req["tp"] = float(tp_price)
            res = mt5.order_send(req)
            d = res._asdict() if res is not None else {}
            ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
            return {"success": ok, "retcode": d.get("retcode"),
                    "comment": d.get("comment"), "order": d.get("order"),
                    "deal": d.get("deal"), "price": d.get("price"),
                    "ticket": d.get("order") or d.get("deal")}
        try:
            return await self._call(_f)
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        def _f(mt5):
            pos = mt5.positions_get()
            out = []
            for p in (pos or []):
                out.append({
                    "ticket": int(p.ticket), "symbol": str(p.symbol),
                    "type": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                    "volume": float(p.volume), "entry_price": float(p.price_open),
                    "current_price": float(p.price_current), "sl": float(p.sl),
                    "tp": float(p.tp), "profit_loss": float(p.profit),
                    "open_time": int(p.time),
                })
            return out
        return await self._call(_f)

    async def get_position_close(self, ticket: int) -> Dict[str, Any]:
        """If position `ticket` is no longer open, return its realized settlement
        from history deals: {closed, exit_price, profit, closed_at}. Returns
        {closed: False} while it's still open or no close deal is resolvable yet."""
        def _f(mt5):
            import datetime as _dt
            if mt5.positions_get(ticket=int(ticket)):
                return {"closed": False}
            deals = mt5.history_deals_get(position=int(ticket))
            if not deals:
                # some builds need an explicit time range alongside position=
                now = int(_dt.datetime.now().timestamp()) + 3600
                deals = mt5.history_deals_get(0, now, position=int(ticket))
            if not deals:
                return {"closed": False}
            outs = [d for d in deals if getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT]
            if not outs:
                return {"closed": False}
            last = outs[-1]
            profit = sum(float(d.profit) for d in outs)
            return {"closed": True, "exit_price": float(last.price), "profit": profit,
                    "closed_at": _dt.datetime.utcfromtimestamp(int(last.time))}
        try:
            return await self._call(_f)
        except Exception as e:
            return {"closed": False, "error": str(e)}

    async def close_position(self, ticket: int) -> Dict[str, Any]:
        def _f(mt5):
            pos = mt5.positions_get(ticket=int(ticket))
            if not pos:
                return {"success": False, "error": "position not found"}
            p = pos[0]
            self._select(mt5, p.symbol)
            t = mt5.symbol_info_tick(p.symbol)
            is_buy = p.type == mt5.POSITION_TYPE_BUY
            req = {
                "action": mt5.TRADE_ACTION_DEAL, "position": int(ticket),
                "symbol": p.symbol, "volume": float(p.volume),
                "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                "price": float(t.bid if is_buy else t.ask),
                "deviation": 20, "magic": 778899, "comment": "hermes-close",
                "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(mt5, p.symbol),
            }
            res = mt5.order_send(req)
            ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
            return {"success": ok, "retcode": getattr(res, "retcode", None),
                    "comment": getattr(res, "comment", None)}
        try:
            return await self._call(_f)
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def set_position_sltp(self, ticket: int, sl_price: Optional[float] = None,
                                tp_price: Optional[float] = None) -> Dict[str, Any]:
        """Attach/modify SL and/or TP on an open position (TRADE_ACTION_SLTP).
        Used to set stops from the *actual* fill price after a market order, so
        spiky symbols (Crash/Boom) never get an SL on the wrong side of the fill."""
        def _f(mt5):
            pos = mt5.positions_get(ticket=int(ticket))
            if not pos:
                return {"success": False, "error": "position not found"}
            p = pos[0]
            req = {"action": mt5.TRADE_ACTION_SLTP, "symbol": p.symbol,
                   "position": int(ticket),
                   "sl": float(sl_price) if sl_price is not None else float(p.sl),
                   "tp": float(tp_price) if tp_price is not None else float(p.tp)}
            res = mt5.order_send(req)
            ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
            return {"success": ok, "retcode": getattr(res, "retcode", None),
                    "comment": getattr(res, "comment", None)}
        try:
            return await self._call(_f)
        except Exception as e:
            return {"success": False, "error": str(e)}
