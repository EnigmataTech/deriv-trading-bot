import json
import math
import asyncio
import websockets
from websockets.protocol import State
import aiohttp
from typing import Dict, Any, Optional, List
import os
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


class DerivAPIClient:
    def __init__(self, app_id: str = None, api_token: str = None):
        self.app_id = app_id or os.getenv("DERIV_APP_ID", "1089")
        self.api_token = api_token or os.getenv("DERIV_API_TOKEN")
        self.ws_url = f"wss://ws.binaryws.com/websockets/v3?app_id={self.app_id}"
        self.websocket = None
        self.request_id = 1
        self._lock = asyncio.Lock()
        
    async def connect(self):
        """Establish WebSocket connection to Deriv API"""
        try:
            self.websocket = await websockets.connect(self.ws_url)
            if self.api_token:
                await self.authorize()
            return True
        except Exception as e:
            print(f"Connection failed: {e}")
            return False
    
    async def disconnect(self):
        """Close WebSocket connection"""
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
    
    async def send_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Send request to Deriv API and return response. Serialized via lock for shared-client safety."""
        async with self._lock:
            if not self.websocket or self.websocket.state != State.OPEN:
                self.websocket = None
                await self.connect()

            request["req_id"] = self.request_id
            self.request_id += 1

            await self.websocket.send(json.dumps(request))
            response = await self.websocket.recv()
            return json.loads(response)
    
    async def authorize(self) -> Dict[str, Any]:
        """Authorize with API token"""
        request = {
            "authorize": self.api_token
        }
        return await self.send_request(request)
    
    async def get_account_balance(self) -> Dict[str, Any]:
        """Get account balance for the authorized account"""
        request = {
            "balance": 1,
            "subscribe": 0
        }
        return await self.send_request(request)
    
    async def get_active_symbols(self, market: str = "forex") -> Dict[str, Any]:
        """Get active trading symbols"""
        request = {
            "active_symbols": "brief",
            "product_type": "basic"
        }
        return await self.send_request(request)
    
    async def get_ticks(self, symbol: str) -> Dict[str, Any]:
        """Get the latest tick for a symbol using one-shot history (avoids subscription streams)."""
        resp = await self.get_ticks_history(symbol, count=1)
        # Reshape to match the tick response format callers expect
        history = resp.get("history", {})
        prices = history.get("prices", [])
        epochs = history.get("times", [])
        if prices:
            return {"tick": {"quote": prices[-1], "epoch": epochs[-1] if epochs else 0, "symbol": symbol}}
        return resp
    
    async def get_ticks_history(self, symbol: str, count: int = 100) -> Dict[str, Any]:
        """Get historical ticks"""
        request = {
            "ticks_history": symbol,
            "count": count,
            "end": "latest",
            "style": "ticks"
        }
        return await self.send_request(request)

    async def get_candles(self, symbol: str, granularity: int = 60, count: int = 50) -> Dict[str, Any]:
        """
        Get OHLC candlestick data for a symbol.

        Args:
            symbol: Trading symbol (e.g., 'R_50')
            granularity: Candle duration in seconds. Supported: 60 (1m), 300 (5m), 900 (15m), 3600 (1h)
            count: Number of candles to return (max 5000)

        Returns:
            Dict with candles array containing {epoch, open, high, low, close} for each candle
        """
        request = {
            "ticks_history": symbol,
            "count": count,
            "end": "latest",
            "style": "candles",
            "granularity": granularity
        }
        return await self.send_request(request)
    
    async def place_contract(self, symbol: str, contract_type: str, amount: float, duration: int = 5, duration_unit: str = "t", currency: str = "USD") -> Dict[str, Any]:
        """Place a binary options contract"""
        request = {
            "buy": 1,
            "price": amount,
            "parameters": {
                "contract_type": contract_type,
                "symbol": symbol,
                "duration": duration,
                "duration_unit": duration_unit,
                "basis": "stake",
                "amount": amount,
                "currency": currency
            }
        }
        return await self.send_request(request)
    
    async def get_portfolio(self) -> Dict[str, Any]:
        """Get portfolio information"""
        request = {
            "portfolio": 1
        }
        return await self.send_request(request)

    async def get_contract_status(self, contract_id: str) -> Dict[str, Any]:
        """
        Get the status of a specific contract by ID.
        Returns contract details including whether it's sold/expired and the profit/loss.
        """
        request = {
            "proposal_open_contract": 1,
            "contract_id": int(contract_id)
        }
        return await self.send_request(request)

    async def get_profit_table(self, limit: int = 50) -> Dict[str, Any]:
        """Get profit/loss table"""
        request = {
            "profit_table": 1,
            "description": 1,
            "limit": limit
        }
        return await self.send_request(request)
    
    async def place_multiplier_contract(
        self,
        symbol: str,
        direction: str,
        amount: float,
        multiplier: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        currency: str = "USD",
    ) -> Dict[str, Any]:
        """Place a multiplier contract. direction: 'MULTUP' (long) or 'MULTDOWN' (short)."""
        params: Dict[str, Any] = {
            "contract_type": direction.upper(),
            "symbol": symbol,
            "amount": amount,
            "multiplier": multiplier,
            "basis": "stake",
            "currency": currency,
        }
        # Deriv's buy `limit_order` takes flat numeric amounts, not the
        # {order_type, order_amount} sub-objects (that shape is only in
        # proposal_open_contract responses). Sending sub-objects fails schema
        # validation ("parameters/limit_order/stop_loss").
        limit_order: Dict[str, Any] = {}
        if stop_loss is not None and stop_loss > 0:
            limit_order["stop_loss"] = stop_loss
        if take_profit is not None and take_profit > 0:
            limit_order["take_profit"] = take_profit
        if limit_order:
            params["limit_order"] = limit_order
        return await self.send_request({"buy": 1, "price": amount, "parameters": params})

    async def sell_contract(self, contract_id: str, price: Optional[float] = None) -> Dict[str, Any]:
        """Sell an open contract at market price (price=0) or specified minimum price."""
        request = {
            "sell": int(contract_id),
            "price": price if price is not None else 0,
        }
        return await self.send_request(request)


class TechnicalIndicators:
    @staticmethod
    def calculate_sma(prices: List[float], period: int) -> List[float]:
        """Calculate Simple Moving Average"""
        sma = []
        for i in range(len(prices)):
            if i < period - 1:
                sma.append(None)
            else:
                avg = sum(prices[i-period+1:i+1]) / period
                sma.append(round(avg, 5))
        return sma
    
    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> List[float]:
        """Calculate Relative Strength Index"""
        if len(prices) < period + 1:
            return [None] * len(prices)
        
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [delta if delta > 0 else 0 for delta in deltas]
        losses = [-delta if delta < 0 else 0 for delta in deltas]
        
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        
        rsi = [None] * (period)
        
        if avg_loss == 0:
            rsi.append(100)
        else:
            rs = avg_gain / avg_loss
            rsi.append(100 - (100 / (1 + rs)))
        
        for i in range(period + 1, len(prices)):
            gain = gains[i-1]
            loss = losses[i-1]
            
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
            
            if avg_loss == 0:
                rsi.append(100)
            else:
                rs = avg_gain / avg_loss
                rsi.append(100 - (100 / (1 + rs)))
        
        return [round(r, 2) if r is not None else None for r in rsi]

    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> List[float]:
        """Exponential Moving Average — seeds from SMA of first `period` values."""
        if len(prices) < period:
            return [None] * len(prices)
        k = 2.0 / (period + 1)
        result: List[float] = [None] * (period - 1)
        seed = sum(prices[:period]) / period
        result.append(round(seed, 5))
        for price in prices[period:]:
            result.append(round(result[-1] * (1 - k) + price * k, 5))
        return result

    @staticmethod
    def calculate_macd(
        prices: List[float],
        fast: int = 12,
        slow: int = 26,
        signal_period: int = 9,
    ) -> tuple:
        """Returns (macd_line, signal_line, histogram) as three parallel lists."""
        fast_ema = TechnicalIndicators.calculate_ema(prices, fast)
        slow_ema = TechnicalIndicators.calculate_ema(prices, slow)

        macd_line = [
            round(f - s, 5) if f is not None and s is not None else None
            for f, s in zip(fast_ema, slow_ema)
        ]

        valid_idx = [i for i, v in enumerate(macd_line) if v is not None]
        valid_vals = [macd_line[i] for i in valid_idx]
        signal_line: List[Optional[float]] = [None] * len(macd_line)
        if len(valid_vals) >= signal_period:
            sig = TechnicalIndicators.calculate_ema(valid_vals, signal_period)
            for j, orig in enumerate(valid_idx):
                signal_line[orig] = sig[j]

        histogram = [
            round(m - s, 5) if m is not None and s is not None else None
            for m, s in zip(macd_line, signal_line)
        ]
        return macd_line, signal_line, histogram

    @staticmethod
    def calculate_bollinger_bands(
        prices: List[float],
        period: int = 20,
        std_mult: float = 2.0,
    ) -> tuple:
        """Returns (upper, middle, lower) as three parallel lists."""
        upper, middle, lower = [], [], []
        for i in range(len(prices)):
            if i < period - 1:
                upper.append(None); middle.append(None); lower.append(None)
            else:
                window = prices[i - period + 1 : i + 1]
                avg = sum(window) / period
                std = math.sqrt(sum((p - avg) ** 2 for p in window) / period)
                middle.append(round(avg, 5))
                upper.append(round(avg + std_mult * std, 5))
                lower.append(round(avg - std_mult * std, 5))
        return upper, middle, lower

    @staticmethod
    def calculate_atr(prices: List[float], period: int = 14) -> List[float]:
        """Simplified ATR using |price[i] - price[i-1]| as True Range proxy for tick data."""
        if len(prices) < 2:
            return [None] * len(prices)
        tr = [None] + [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
        atr: List[Optional[float]] = [None] * len(prices)
        if len(tr) < period + 1:
            return atr
        seed = sum(tr[1 : period + 1]) / period
        atr[period] = round(seed, 5)
        k = 1.0 / period  # Wilder's smoothing
        for i in range(period + 1, len(tr)):
            atr[i] = round(atr[i - 1] * (1 - k) + tr[i] * k, 5)
        return atr


class DerivTickStream:
    """Dedicated subscription client for real-time tick streaming.
    Separate from DerivAPIClient so subscriptions never block request-response traffic."""

    def __init__(self):
        self.app_id = os.getenv("DERIV_APP_ID", "1089")
        self.api_token = os.getenv("DERIV_API_TOKEN")
        self.ws_url = f"wss://ws.binaryws.com/websockets/v3?app_id={self.app_id}"
        self._prices: Dict[str, float] = {}
        self._running = False

    @property
    def prices(self) -> Dict[str, float]:
        return dict(self._prices)

    async def run(self, symbols: List[str]) -> None:
        """Subscribe to symbols and stream ticks into _prices cache. Reconnects on error."""
        self._running = True
        while self._running:
            try:
                ws = await websockets.connect(self.ws_url)
                try:
                    # Authenticate
                    if self.api_token:
                        await ws.send(json.dumps({"authorize": self.api_token, "req_id": 0}))
                        await ws.recv()
                    # Subscribe to all symbols at once
                    for i, sym in enumerate(symbols, start=1):
                        await ws.send(json.dumps({"ticks": sym, "subscribe": 1, "req_id": i}))
                    # Stream incoming ticks
                    async for raw in ws:
                        msg = json.loads(raw)
                        tick = msg.get("tick", {})
                        sym = tick.get("symbol")
                        quote = tick.get("quote")
                        if sym and quote is not None:
                            self._prices[sym] = float(quote)
                finally:
                    await ws.close()
            except Exception:
                await asyncio.sleep(5)  # back-off before reconnecting
