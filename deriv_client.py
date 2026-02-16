import json
import asyncio
import websockets
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
    
    async def send_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Send request to Deriv API and return response"""
        if not self.websocket:
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
        """Get real-time ticks for a symbol"""
        request = {
            "ticks": symbol
        }
        return await self.send_request(request)
    
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
    
    async def sell_contract(self, contract_id: str, price: Optional[float] = None) -> Dict[str, Any]:
        """Sell an open contract"""
        request = {
            "sell": contract_id
        }
        if price:
            request["price"] = price
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