"""Pure technical-indicator math (RSI / SMA / EMA / MACD / Bollinger / ATR).

Broker-agnostic: takes a list of prices, returns parallel lists. Extracted from
the old deriv_client module during the MT5 migration so the signal engine has no
dependency on the (decommissioned) Deriv WebSocket stack.
"""
import math
from typing import List, Optional


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
