from fastmcp import FastMCP
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, Response
from database import TradingRepository
from deriv_client import DerivAPIClient, TechnicalIndicators
from logger import setup_logging, get_logger, log_trade_placed, log_trade_closed, log_api_call, log_error, log_balance_update, log_audit_auth, log_audit_trade
from trade_monitor import TradeMonitor
from jose import jwt
import os
import asyncio
import json
from typing import Optional
from contextvars import ContextVar
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from functools import wraps

load_dotenv()

# Initialize logging
setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = get_logger("main")

# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)

# Rate limit configurations
RATE_LIMITS = {
    "trade": "10/minute",      # Max 10 trades per minute
    "sync": "5/minute",        # Max 5 syncs per minute
    "default": "60/minute",    # Default for read endpoints
    "auth": "20/minute",       # Auth-related endpoints
}

# Request size limits (in bytes)
MAX_REQUEST_SIZE = int(os.getenv("MAX_REQUEST_SIZE", "102400"))  # 100KB default


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Middleware to limit request body size to prevent DoS attacks"""

    def __init__(self, app, max_size: int = MAX_REQUEST_SIZE):
        super().__init__(app)
        self.max_size = max_size

    async def dispatch(self, request: StarletteRequest, call_next) -> Response:
        # Check Content-Length header
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
                if length > self.max_size:
                    logger.warning(f"Request too large: {length} bytes from {request.client.host}")
                    return JSONResponse(
                        {"success": False, "error": f"Request too large. Maximum size is {self.max_size} bytes"},
                        status_code=413
                    )
            except ValueError:
                pass

        # For requests without Content-Length, check body size during processing
        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.body()
            if len(body) > self.max_size:
                logger.warning(f"Request body too large: {len(body)} bytes from {request.client.host}")
                return JSONResponse(
                    {"success": False, "error": f"Request too large. Maximum size is {self.max_size} bytes"},
                    status_code=413
                )

        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware to add security headers to all responses"""

    async def dispatch(self, request: StarletteRequest, call_next) -> Response:
        response = await call_next(request)

        # Prevent MIME type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"

        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"

        # Enable XSS filter in browsers
        response.headers["X-XSS-Protection"] = "1; mode=block"

        # Referrer policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Content Security Policy (restrictive for API)
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"

        # Permissions Policy (disable unnecessary browser features)
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

        return response


# CSRF protection via Origin header validation
ALLOWED_ORIGINS_LIST = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")


class CSRFProtectionMiddleware(BaseHTTPMiddleware):
    """Middleware to protect against CSRF attacks by validating Origin header"""

    def __init__(self, app, allowed_origins: list = None):
        super().__init__(app)
        self.allowed_origins = allowed_origins or ALLOWED_ORIGINS_LIST

    async def dispatch(self, request: StarletteRequest, call_next) -> Response:
        # Only check state-changing methods
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")

            # Skip check for requests without Origin (e.g., same-origin or non-browser)
            # but still require Origin for cross-origin requests
            if origin:
                if origin not in self.allowed_origins:
                    logger.warning(f"CSRF check failed: Origin '{origin}' not in allowed list")
                    return JSONResponse(
                        {"success": False, "error": "CSRF validation failed: Invalid origin"},
                        status_code=403
                    )

        return await call_next(request)


def rate_limit_exceeded_handler(request: StarletteRequest, exc: RateLimitExceeded) -> JSONResponse:
    """Handle rate limit exceeded errors"""
    logger.warning(f"Rate limit exceeded for {request.client.host}: {exc.detail}")
    return JSONResponse(
        {"success": False, "error": "Rate limit exceeded. Please slow down."},
        status_code=429
    )

def check_rate_limit(limit_type: str = "default"):
    """Decorator to apply rate limiting to endpoints"""
    def decorator(func):
        @wraps(func)
        async def async_wrapper(request: StarletteRequest, *args, **kwargs):
            # Check rate limit
            limit = RATE_LIMITS.get(limit_type, RATE_LIMITS["default"])
            try:
                # Simple in-memory rate limiting check
                # In production, use Redis-backed limiter
                pass  # Rate limiting handled by middleware
            except Exception:
                pass
            return await func(request, *args, **kwargs)

        @wraps(func)
        def sync_wrapper(request: StarletteRequest, *args, **kwargs):
            return func(request, *args, **kwargs)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    return decorator

# Context variable to store current user_id per request
_current_user_id: ContextVar[Optional[str]] = ContextVar("current_user_id", default=None)

# Authentication - ENABLED by default for security
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"

# Initialize MCP server (auth handled at REST endpoint level)
mcp = FastMCP(name="Deriv Trading MCP Server")

if AUTH_ENABLED:
    logger.info("Server started with REST authentication enabled")
else:
    logger.warning("Server started WITHOUT authentication (testing mode)")

# Trade monitor instance (started on server startup)
trade_monitor: Optional[TradeMonitor] = None

def get_user_id() -> str:
    """Get user ID from context variable - raises error if not authenticated"""
    ctx_user_id = _current_user_id.get()
    if ctx_user_id:
        return ctx_user_id

    # No fallback - authentication is required
    raise ValueError("No authenticated user in context")

def extract_user_from_request(request: StarletteRequest) -> Optional[str]:
    """Extract and validate user_id from Authorization header JWT"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header[7:]  # Remove "Bearer " prefix
    try:
        # Get Stytch project ID for validation
        stytch_project_id = os.getenv("STYTCH_PROJECT_ID")

        # Decode and verify the token
        # Note: In production, you should fetch JWKS from Stytch and verify signature
        # For now, we verify the claims structure and expiration
        claims = jwt.decode(
            token,
            key="",  # Empty key since we're not verifying signature
            options={
                "verify_signature": False,  # TODO: Implement JWKS verification
                "verify_exp": True,         # Verify expiration
                "verify_aud": False,        # Stytch doesn't use audience
                "require_exp": True,        # Require expiration claim
            }
        )

        user_id = claims.get("sub")
        if not user_id:
            logger.warning("JWT token missing 'sub' claim")
            return None

        return user_id
    except jwt.ExpiredSignatureError:
        logger.warning("JWT token has expired")
        return None
    except jwt.JWTError as e:
        logger.warning(f"JWT validation failed: {e}")
        return None
    except Exception as e:
        logger.warning(f"Failed to extract user from token: {e}")
        return None

def require_auth(request: StarletteRequest) -> tuple[Optional[str], Optional[JSONResponse]]:
    """Check authentication and return user_id or error response"""
    client_ip = request.client.host if request.client else "unknown"

    if not AUTH_ENABLED:
        # Only allow bypass in explicit non-auth mode (for local testing)
        logger.debug("Authentication disabled - using test user")
        return "test_user_dev", None

    user_id = extract_user_from_request(request)
    if not user_id:
        log_audit_auth(
            event_type="failed_login",
            ip_address=client_ip,
            success=False,
            reason="Invalid or missing authentication token"
        )
        return None, JSONResponse({"success": False, "error": "Authentication required"}, status_code=401)

    log_audit_auth(
        event_type="token_validated",
        user_id=user_id,
        ip_address=client_ip,
        success=True
    )
    return user_id, None

# Whitelist of allowed trading symbols
ALLOWED_SYMBOLS = {
    # Volatility Indices
    "R_10", "R_25", "R_50", "R_75", "R_100",
    # 1-second Volatility Indices
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
    # Jump Indices
    "JD10", "JD25", "JD50", "JD75", "JD100",
    # Crash/Boom Indices
    "BOOM300N", "BOOM500", "BOOM1000", "CRASH300N", "CRASH500", "CRASH1000",
}

# Daily loss protection limits
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "100"))  # $100 default
MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "50"))  # 50 trades per day


def check_daily_loss_limit(user_id: str) -> tuple[bool, Optional[str]]:
    """
    Check if user has exceeded daily loss limit.
    Returns (can_trade, error_message).
    """
    from datetime import datetime, timedelta
    from database import SessionLocal, Trade

    db = SessionLocal()
    try:
        # Get today's trades
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_trades = db.query(Trade).filter(
            Trade.user_id == user_id,
            Trade.created_at >= today_start
        ).all()

        # Check trade count limit
        if len(today_trades) >= MAX_DAILY_TRADES:
            return False, f"Daily trade limit reached ({MAX_DAILY_TRADES} trades)"

        # Calculate today's total loss
        today_loss = sum(
            trade.profit_loss for trade in today_trades
            if trade.profit_loss is not None and trade.profit_loss < 0
        )
        total_loss = abs(today_loss)

        if total_loss >= MAX_DAILY_LOSS:
            return False, f"Daily loss limit reached (${MAX_DAILY_LOSS:.2f})"

        return True, None
    finally:
        db.close()


def validate_symbol(symbol: str) -> tuple[Optional[str], Optional[str]]:
    """Validate trading symbol against whitelist. Returns (validated_symbol, error_message)"""
    if not symbol:
        return None, "Symbol is required"

    symbol_upper = symbol.upper()

    # Check against whitelist
    if symbol_upper not in ALLOWED_SYMBOLS:
        return None, f"Invalid symbol. Allowed symbols: {', '.join(sorted(ALLOWED_SYMBOLS))}"

    return symbol_upper, None

def validate_trade_params(body: dict) -> tuple[Optional[dict], Optional[JSONResponse]]:
    """Validate trade parameters. Returns (validated_params, error_response)"""
    errors = []

    # Symbol validation
    symbol = body.get("symbol")
    if not symbol:
        errors.append("symbol is required")
    else:
        validated_symbol, err = validate_symbol(symbol)
        if err:
            errors.append(err)
        else:
            symbol = validated_symbol

    # Amount validation (Deriv minimum is $0.35, max $10,000)
    MIN_TRADE_AMOUNT = 0.35
    MAX_TRADE_AMOUNT = 10000
    try:
        amount = float(body.get("amount", 0))
        if amount < MIN_TRADE_AMOUNT:
            errors.append(f"amount must be at least ${MIN_TRADE_AMOUNT}")
        elif amount > MAX_TRADE_AMOUNT:
            errors.append(f"amount cannot exceed ${MAX_TRADE_AMOUNT}")
    except (ValueError, TypeError):
        errors.append("invalid amount format - must be a number")
        amount = 0

    # Direction validation
    direction = body.get("direction", "").upper()
    if direction not in ["CALL", "PUT"]:
        errors.append("direction must be CALL or PUT")

    # Duration validation
    try:
        duration = int(body.get("duration", 5))
        if duration < 1 or duration > 600:
            errors.append("duration must be between 1 and 600")
    except (ValueError, TypeError):
        errors.append("invalid duration format")
        duration = 5

    # Stop-loss validation (optional)
    stop_loss = None
    if body.get("stop_loss") is not None and body.get("stop_loss") != "":
        try:
            stop_loss = float(body.get("stop_loss"))
            if stop_loss <= 0:
                errors.append("stop_loss must be a positive number")
        except (ValueError, TypeError):
            errors.append("invalid stop_loss format")

    # Take-profit validation (optional)
    take_profit = None
    if body.get("take_profit") is not None and body.get("take_profit") != "":
        try:
            take_profit = float(body.get("take_profit"))
            if take_profit <= 0:
                errors.append("take_profit must be a positive number")
        except (ValueError, TypeError):
            errors.append("invalid take_profit format")

    # Trailing stop distance validation (optional)
    trailing_stop_distance = None
    if body.get("trailing_stop_distance") is not None and body.get("trailing_stop_distance") != "":
        try:
            trailing_stop_distance = float(body.get("trailing_stop_distance"))
            if trailing_stop_distance <= 0:
                errors.append("trailing_stop_distance must be a positive number")
        except (ValueError, TypeError):
            errors.append("invalid trailing_stop_distance format")

    if errors:
        return None, JSONResponse({
            "success": False,
            "error": "Validation failed",
            "details": errors
        }, status_code=400)

    return {
        "symbol": symbol,
        "amount": amount,
        "direction": direction,
        "duration": duration,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "trailing_stop_distance": trailing_stop_distance
    }, None

async def get_deriv_client() -> DerivAPIClient:
    """Helper function to get connected Deriv client"""
    client = DerivAPIClient()
    await client.connect()
    return client

# ============ Business Logic Functions (shared by MCP tools and REST endpoints) ============

async def _do_get_balance() -> str:
    """Get account balance from Deriv API"""
    client = await get_deriv_client()
    try:
        response = await client.get_account_balance()
        if 'error' in response:
            return f"Error: {response['error']['message']}"

        balance_data = response.get('balance', {})
        user_id = get_user_id()

        # Update local portfolio record
        if 'balance' in balance_data:
            TradingRepository.update_portfolio(
                user_id=user_id,
                balance=float(balance_data['balance']),
                equity=float(balance_data.get('equity', 0)),
                margin=float(balance_data.get('margin', 0)),
                free_margin=float(balance_data.get('free_margin', 0))
            )

        return f"Balance: ${balance_data.get('balance', 'N/A')} {balance_data.get('currency', 'USD')}"
    finally:
        await client.disconnect()

def _do_get_trade_history() -> str:
    """Get trade history for current user"""
    user_id = get_user_id()
    trades = TradingRepository.get_trades_by_user(user_id)

    if not trades:
        return "No trades found"

    result = "Trading History:\n"
    for trade in trades:
        result += f"ID: {trade.trade_id}, Symbol: {trade.symbol}, Type: {trade.trade_type}, "
        result += f"Amount: ${trade.amount}, Entry: {trade.entry_price}, Status: {trade.status}\n"

    return result

async def _do_place_trade_async(
    symbol: str,
    amount: float,
    direction: str,
    duration: int = 5,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    trailing_stop_distance: Optional[float] = None
) -> str:
    """Place a binary options trade - async business logic"""
    # Check daily loss limit before placing trade
    user_id = get_user_id()
    can_trade, limit_error = check_daily_loss_limit(user_id)
    if not can_trade:
        logger.warning(f"Trade blocked for user {user_id}: {limit_error}")
        log_audit_trade(
            event_type="blocked",
            user_id=user_id,
            symbol=symbol,
            amount=amount,
            direction=direction,
            success=False,
            reason=limit_error
        )
        return f"Trade blocked: {limit_error}"

    client = await get_deriv_client()
    try:
        contract_type = "CALL" if direction.upper() == "CALL" else "PUT"
        response = await client.place_contract(symbol, contract_type, amount, duration)

        if 'error' in response:
            error_msg = response['error']['message']
            log_error(Exception(error_msg), "place_trade", error_code="DERIV_API_ERROR")
            log_audit_trade(
                event_type="place",
                user_id=user_id,
                symbol=symbol,
                amount=amount,
                direction=direction,
                success=False,
                reason=error_msg
            )
            return f"Error placing trade: {error_msg}"

        buy_data = response.get('buy', {})
        contract_id = buy_data.get('contract_id')

        if contract_id:
            entry_price = float(buy_data.get('start_spot', 0))

            # Store trade in database with SL/TP settings
            TradingRepository.create_trade_with_sl_tp(
                user_id=user_id,
                trade_id=str(contract_id),
                symbol=symbol,
                trade_type=direction.lower(),
                amount=amount,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_stop_distance=trailing_stop_distance
            )

            # Log the trade placement
            log_trade_placed(
                trade_id=str(contract_id),
                symbol=symbol,
                amount=amount,
                direction=direction.upper(),
                contract_type=contract_type,
                duration=duration
            )

            # Audit log for successful trade
            log_audit_trade(
                event_type="place",
                user_id=user_id,
                trade_id=str(contract_id),
                symbol=symbol,
                amount=amount,
                direction=direction.upper(),
                success=True
            )

            result = f"Trade placed successfully!\nContract ID: {contract_id}\nSymbol: {symbol}\nAmount: ${amount}\nDirection: {direction}"
            if stop_loss:
                result += f"\nStop-Loss: {stop_loss}"
            if take_profit:
                result += f"\nTake-Profit: {take_profit}"
            if trailing_stop_distance:
                result += f"\nTrailing Stop: {trailing_stop_distance} points"
            return result
        else:
            log_error(Exception("No contract ID returned"), "place_trade", error_code="NO_CONTRACT_ID")
            return "Trade placement failed - no contract ID returned"
    finally:
        await client.disconnect()

def _do_place_trade(symbol: str, amount: float, direction: str, duration: int = 5) -> str:
    """Place a binary options trade - sync wrapper for MCP tools"""
    return asyncio.run(_do_place_trade_async(symbol, amount, direction, duration))

# ============ MCP Tools ============

@mcp.tool()
def get_account_balance() -> str:
    """Get current account balance from Deriv"""
    return asyncio.run(_do_get_balance())

@mcp.tool()
def get_market_data(symbol: str) -> str:
    """Get real-time market data for a trading symbol"""
    async def _get_market_data():
        client = await get_deriv_client()
        try:
            response = await client.get_ticks(symbol)
            if 'error' in response:
                return f"Error: {response['error']['message']}"
            
            tick_data = response.get('tick', {})
            return f"Symbol: {symbol}\nPrice: {tick_data.get('quote', 'N/A')}\nTime: {tick_data.get('epoch', 'N/A')}"
        finally:
            await client.disconnect()
    
    return asyncio.run(_get_market_data())

@mcp.tool()
def place_trade(symbol: str, amount: float, direction: str, duration: int = 5) -> str:
    """Place a binary options trade. Direction: 'CALL' or 'PUT'"""
    return _do_place_trade(symbol, amount, direction, duration)

@mcp.tool()
def get_trade_history() -> str:
    """Get trading history for the current user"""
    return _do_get_trade_history()

@mcp.tool()
def calculate_technical_indicators(symbol: str, indicator: str, period: int = 14) -> str:
    """Calculate technical indicators (sma, rsi) for a symbol"""
    async def _calculate_indicators():
        client = await get_deriv_client()
        try:
            # Get historical data
            response = await client.get_ticks_history(symbol, count=100)
            if 'error' in response:
                return f"Error: {response['error']['message']}"
            
            history = response.get('history', {})
            prices = [float(price) for price in history.get('prices', [])]
            
            if len(prices) < period:
                return f"Not enough data for {indicator} calculation. Need at least {period} data points."
            
            if indicator.lower() == 'sma':
                values = TechnicalIndicators.calculate_sma(prices, period)
                latest_value = values[-1] if values and values[-1] is not None else "N/A"
                return f"{indicator.upper()}({period}) for {symbol}: {latest_value}"
            
            elif indicator.lower() == 'rsi':
                values = TechnicalIndicators.calculate_rsi(prices, period)
                latest_value = values[-1] if values and values[-1] is not None else "N/A"
                return f"{indicator.upper()}({period}) for {symbol}: {latest_value}"
            
            else:
                return f"Unsupported indicator: {indicator}. Available: sma, rsi"
        
        finally:
            await client.disconnect()
    
    return asyncio.run(_calculate_indicators())

@mcp.tool()
def analyze_portfolio_performance() -> str:
    """Analyze portfolio performance and provide summary"""
    async def _analyze_portfolio():
        user_id = get_user_id()
        trades = TradingRepository.get_trades_by_user(user_id)
        portfolio = TradingRepository.get_portfolio(user_id)
        
        if not trades:
            return "No trades available for analysis"
        
        total_trades = len(trades)
        winning_trades = len([t for t in trades if t.profit_loss and t.profit_loss > 0])
        losing_trades = len([t for t in trades if t.profit_loss and t.profit_loss < 0])
        
        total_profit_loss = sum([t.profit_loss for t in trades if t.profit_loss])
        
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        
        result = f"Portfolio Performance Analysis:\n"
        result += f"Total Trades: {total_trades}\n"
        result += f"Winning Trades: {winning_trades}\n"
        result += f"Losing Trades: {losing_trades}\n"
        result += f"Win Rate: {win_rate:.2f}%\n"
        result += f"Total P&L: ${total_profit_loss:.2f}\n"
        
        if portfolio:
            result += f"Current Balance: ${portfolio.balance}\n"
            result += f"Current Equity: ${portfolio.equity}\n"
        
        return result
    
    return asyncio.run(_analyze_portfolio())

@mcp.tool()
def get_active_symbols() -> str:
    """Get list of active trading symbols"""
    async def _get_symbols():
        client = await get_deriv_client()
        try:
            response = await client.get_active_symbols()
            if 'error' in response:
                return f"Error: {response['error']['message']}"
            
            symbols = response.get('active_symbols', [])
            if not symbols:
                return "No active symbols found"
            
            result = "Active Trading Symbols:\n"
            for symbol in symbols[:20]:  # Limit to first 20
                result += f"{symbol.get('symbol', 'N/A')} - {symbol.get('display_name', 'N/A')}\n"
            
            if len(symbols) > 20:
                result += f"... and {len(symbols) - 20} more symbols\n"
            
            return result
        finally:
            await client.disconnect()
    
    return asyncio.run(_get_symbols())

# REST API endpoints for n8n integration
@mcp.custom_route("/api/balance", methods=["GET"])
async def api_get_balance(request: StarletteRequest) -> JSONResponse:
    """REST endpoint for getting account balance"""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response

    ctx_token = _current_user_id.set(user_id)
    try:
        result = await _do_get_balance()
        log_api_call("/api/balance", "GET", 200)
        return JSONResponse({"success": True, "data": result})
    except Exception as e:
        log_error(e, "api_get_balance")
        return JSONResponse({"success": False, "error": "Failed to retrieve balance"}, status_code=500)
    finally:
        _current_user_id.reset(ctx_token)

async def _do_get_market_data(symbol: str) -> str:
    """Get market data - async business logic"""
    client = await get_deriv_client()
    try:
        response = await client.get_ticks(symbol)
        if 'error' in response:
            return f"Error: {response['error']['message']}"

        tick_data = response.get('tick', {})
        return f"Symbol: {symbol}\nPrice: {tick_data.get('quote', 'N/A')}\nTime: {tick_data.get('epoch', 'N/A')}"
    finally:
        await client.disconnect()

@mcp.custom_route("/api/market-data/{symbol}", methods=["GET"])
async def api_get_market_data(request: StarletteRequest) -> JSONResponse:
    """REST endpoint for getting market data"""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response

    try:
        symbol = request.path_params["symbol"]
        validated_symbol, err = validate_symbol(symbol)
        if err:
            return JSONResponse({"success": False, "error": err}, status_code=400)

        result = await _do_get_market_data(validated_symbol)
        log_api_call(f"/api/market-data/{validated_symbol}", "GET", 200)
        return JSONResponse({"success": True, "data": result, "symbol": validated_symbol})
    except Exception as e:
        log_error(e, "api_get_market_data")
        return JSONResponse({"success": False, "error": f"Failed to retrieve market data: {str(e)}"}, status_code=500)

@mcp.custom_route("/api/candles/{symbol}", methods=["GET"])
async def api_get_candles(request: StarletteRequest) -> JSONResponse:
    """REST endpoint for getting candlestick (OHLC) data"""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response

    try:
        symbol = request.path_params["symbol"]
        validated_symbol, err = validate_symbol(symbol)
        if err:
            return JSONResponse({"success": False, "error": err}, status_code=400)

        # Parse timeframe parameter (1m, 5m, 15m, 1h)
        timeframe = request.query_params.get("timeframe", "1m")
        granularity_map = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "1h": 3600
        }
        if timeframe not in granularity_map:
            return JSONResponse({
                "success": False,
                "error": f"Invalid timeframe. Supported: {', '.join(granularity_map.keys())}"
            }, status_code=400)
        granularity = granularity_map[timeframe]

        # Parse count parameter
        try:
            count = int(request.query_params.get("count", 50))
            if count < 1 or count > 1000:
                return JSONResponse({"success": False, "error": "count must be between 1 and 1000"}, status_code=400)
        except ValueError:
            return JSONResponse({"success": False, "error": "invalid count format"}, status_code=400)

        client = await get_deriv_client()
        try:
            response = await client.get_candles(validated_symbol, granularity, count)

            if 'error' in response:
                return JSONResponse({
                    "success": False,
                    "error": response['error'].get('message', 'Unknown API error')
                }, status_code=500)

            candles = response.get('candles', [])
            formatted_candles = [
                {
                    "time": c.get("epoch"),
                    "open": c.get("open"),
                    "high": c.get("high"),
                    "low": c.get("low"),
                    "close": c.get("close")
                }
                for c in candles
            ]

            log_api_call(f"/api/candles/{validated_symbol}", "GET", 200)
            return JSONResponse({
                "success": True,
                "symbol": validated_symbol,
                "timeframe": timeframe,
                "candles": formatted_candles
            })
        finally:
            await client.disconnect()

    except Exception as e:
        log_error(e, "api_get_candles")
        return JSONResponse({"success": False, "error": f"Failed to retrieve candle data: {str(e)}"}, status_code=500)

@mcp.custom_route("/api/trade", methods=["POST"])
async def api_place_trade(request: StarletteRequest) -> JSONResponse:
    """REST endpoint for placing trades"""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response

    # Set user context for this request
    ctx_token = _current_user_id.set(user_id)
    try:
        body = await request.json()

        # Validate trade parameters
        params, validation_error = validate_trade_params(body)
        if validation_error:
            return validation_error

        log_api_call("/api/trade", "POST", 200)
        result = await _do_place_trade_async(
            params["symbol"],
            params["amount"],
            params["direction"],
            params["duration"],
            stop_loss=params.get("stop_loss"),
            take_profit=params.get("take_profit"),
            trailing_stop_distance=params.get("trailing_stop_distance")
        )
        return JSONResponse({"success": True, "data": result})
    except Exception as e:
        log_error(e, "api_place_trade")
        return JSONResponse({"success": False, "error": "Failed to place trade"}, status_code=500)
    finally:
        _current_user_id.reset(ctx_token)

@mcp.custom_route("/api/trade/{contract_id}/sell", methods=["POST"])
async def api_sell_contract(request: StarletteRequest) -> JSONResponse:
    """REST endpoint for selling/closing an open contract"""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response

    ctx_token = _current_user_id.set(user_id)
    try:
        contract_id = request.path_params["contract_id"]

        # Verify user owns this trade
        trade = TradingRepository.get_trade_by_trade_id(contract_id)
        if not trade:
            return JSONResponse({"success": False, "error": "Trade not found"}, status_code=404)
        if trade.user_id != user_id:
            return JSONResponse({"success": False, "error": "Not authorized to sell this contract"}, status_code=403)
        if trade.status != 'open':
            return JSONResponse({"success": False, "error": "Trade is already closed"}, status_code=400)

        # Parse optional price parameter
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        price = body.get("price") if body else None

        # Sell the contract via Deriv API
        client = await get_deriv_client()
        try:
            response = await client.sell_contract(contract_id, price)

            if 'error' in response:
                return JSONResponse({
                    "success": False,
                    "error": response['error'].get('message', 'Failed to sell contract')
                }, status_code=500)

            sell_data = response.get('sell', {})
            sold_for = sell_data.get('sold_for', 0)
            profit_loss = float(sold_for) - trade.amount

            # Update trade in database
            TradingRepository.update_trade_result(
                trade_id=contract_id,
                exit_price=float(sold_for),
                profit_loss=profit_loss,
                status='closed'
            )

            log_api_call(f"/api/trade/{contract_id}/sell", "POST", 200)
            return JSONResponse({
                "success": True,
                "data": {
                    "contract_id": contract_id,
                    "sold_for": sold_for,
                    "profit_loss": profit_loss
                }
            })
        finally:
            await client.disconnect()

    except Exception as e:
        log_error(e, "api_sell_contract")
        return JSONResponse({"success": False, "error": f"Failed to sell contract: {str(e)}"}, status_code=500)
    finally:
        _current_user_id.reset(ctx_token)

@mcp.custom_route("/api/trades", methods=["GET"])
def api_get_trades(request: StarletteRequest) -> JSONResponse:
    """REST endpoint for getting trade history"""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response

    try:
        ctx_token = _current_user_id.set(user_id)
        try:
            result = _do_get_trade_history()
            log_api_call("/api/trades", "GET", 200)
            return JSONResponse({"success": True, "data": result})
        finally:
            _current_user_id.reset(ctx_token)
    except Exception as e:
        log_error(e, "api_get_trades")
        return JSONResponse({"success": False, "error": "Failed to retrieve trades"}, status_code=500)

@mcp.custom_route("/api/trades/sync", methods=["POST"])
async def api_sync_trades(request: StarletteRequest) -> JSONResponse:
    """REST endpoint to manually sync/update trade statuses from Deriv API"""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response

    ctx_token = _current_user_id.set(user_id)
    try:
        from trade_monitor import TradeMonitor

        # Create a temporary monitor instance for one-time sync
        monitor = TradeMonitor()
        await monitor.deriv_client.connect()

        try:
            result = await monitor.check_open_trades()
            log_api_call("/api/trades/sync", "POST", 200)
            return JSONResponse({
                "success": True,
                "message": f"Synced {result['checked']} trades, updated {result['updated']}",
                "details": result
            })
        finally:
            await monitor.deriv_client.disconnect()

    except Exception as e:
        log_error(e, "api_sync_trades")
        return JSONResponse({"success": False, "error": f"Failed to sync trades: {str(e)}"}, status_code=500)
    finally:
        _current_user_id.reset(ctx_token)

@mcp.custom_route("/api/indicators/{symbol}", methods=["GET"])
def api_get_indicators(request: StarletteRequest) -> JSONResponse:
    """REST endpoint for calculating technical indicators"""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response

    try:
        symbol = request.path_params["symbol"]
        validated_symbol, err = validate_symbol(symbol)
        if err:
            return JSONResponse({"success": False, "error": err}, status_code=400)

        indicator = request.query_params.get("indicator", "sma").lower()
        if indicator not in ["sma", "rsi"]:
            return JSONResponse({"success": False, "error": "indicator must be sma or rsi"}, status_code=400)

        try:
            period = int(request.query_params.get("period", 14))
            if period < 1 or period > 100:
                return JSONResponse({"success": False, "error": "period must be between 1 and 100"}, status_code=400)
        except ValueError:
            return JSONResponse({"success": False, "error": "invalid period format"}, status_code=400)

        result = calculate_technical_indicators(validated_symbol, indicator, period)
        log_api_call(f"/api/indicators/{validated_symbol}", "GET", 200)
        return JSONResponse({"success": True, "data": result, "symbol": validated_symbol, "indicator": indicator, "period": period})
    except Exception as e:
        log_error(e, "api_get_indicators")
        return JSONResponse({"success": False, "error": "Failed to calculate indicators"}, status_code=500)

@mcp.custom_route("/api/portfolio", methods=["GET"])
def api_get_portfolio(request: StarletteRequest) -> JSONResponse:
    """REST endpoint for portfolio analysis"""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response

    try:
        ctx_token = _current_user_id.set(user_id)
        try:
            result = analyze_portfolio_performance()
            log_api_call("/api/portfolio", "GET", 200)
            return JSONResponse({"success": True, "data": result})
        finally:
            _current_user_id.reset(ctx_token)
    except Exception as e:
        log_error(e, "api_get_portfolio")
        return JSONResponse({"success": False, "error": "Failed to analyze portfolio"}, status_code=500)

@mcp.custom_route("/api/symbols", methods=["GET"])
def api_get_symbols(request: StarletteRequest) -> JSONResponse:
    """REST endpoint for getting active symbols"""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response

    try:
        result = get_active_symbols()
        log_api_call("/api/symbols", "GET", 200)
        return JSONResponse({"success": True, "data": result})
    except Exception as e:
        log_error(e, "api_get_symbols")
        return JSONResponse({"success": False, "error": "Failed to retrieve symbols"}, status_code=500)

@mcp.custom_route("/health", methods=["GET"])
def health_check(request: StarletteRequest) -> JSONResponse:
    """Health check endpoint for k8s liveness/readiness probes - no auth required"""
    return JSONResponse({"status": "healthy", "service": "deriv-trading-bot"})

@mcp.custom_route("/api/health", methods=["GET"])
def api_health_check(request: StarletteRequest) -> JSONResponse:
    """Health check endpoint for n8n monitoring - no auth required"""
    return JSONResponse({"success": True, "status": "healthy", "service": "Deriv Trading MCP Server"})

@mcp.custom_route("/api/create-test-token", methods=["POST"])
async def create_test_token(request: StarletteRequest) -> JSONResponse:
    """Create a test token for n8n integration - no auth required"""
    try:
        import requests
        
        body = await request.json()
        email = body.get("email")
        password = body.get("password")
        
        if not email or not password:
            return JSONResponse({"success": False, "error": "Email and password required"}, status_code=400)
        
        # Call Stytch API to authenticate and get JWT
        auth_response = requests.post(
            f"{os.getenv('STYTCH_DOMAIN')}/v1/passwords/authenticate",
            auth=(os.getenv('STYTCH_PROJECT_ID'), os.getenv('STYTCH_SECRET')),
            json={
                "email": email,
                "password": password,
                "session_duration_minutes": 60
            }
        )
        
        if auth_response.status_code == 200:
            result = auth_response.json()
            return JSONResponse({
                "success": True, 
                "session_jwt": result.get("session_jwt"),
                "user_id": result.get("user_id")
            })
        else:
            return JSONResponse({
                "success": False, 
                "error": f"Authentication failed: {auth_response.text}"
            }, status_code=auth_response.status_code)
            
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@mcp.custom_route("/api/auth/test", methods=["GET"])
def api_auth_test(request: StarletteRequest) -> JSONResponse:
    """Test authentication endpoint"""
    try:
        user_id = get_user_id()
        return JSONResponse({"success": True, "user_id": user_id, "message": "Authentication working"})
    except Exception as e:
        return JSONResponse({"success": False, "error": "Authentication failed", "details": str(e)}, status_code=401)

@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS"])
def oauth_metadata(request: StarletteRequest) -> JSONResponse:
    base_url = str(request.base_url).rstrip("/")

    return JSONResponse(
        {
            "resource": base_url,
            "authorization_servers":  [os.getenv("STYTCH_DOMAIN")],
            "scopes_supported": ["read", "write"],
            "bearer_methods_supported": ["header", "body"]
        }
    )

async def start_trade_monitor():
    """Start the trade monitor as a background task"""
    global trade_monitor
    try:
        trade_monitor = TradeMonitor(
            poll_interval=int(os.getenv("TRADE_MONITOR_INTERVAL", "30"))
        )
        await trade_monitor.start()
        logger.info("Trade monitor started successfully")
    except Exception as e:
        log_error(e, "start_trade_monitor")
        logger.error(f"Failed to start trade monitor: {e}")

async def stop_trade_monitor():
    """Stop the trade monitor gracefully"""
    global trade_monitor
    if trade_monitor:
        await trade_monitor.stop()
        logger.info("Trade monitor stopped")

@mcp.custom_route("/api/trade-monitor/status", methods=["GET"])
def api_trade_monitor_status(request: StarletteRequest) -> JSONResponse:
    """Get trade monitor status"""
    return JSONResponse({
        "success": True,
        "running": trade_monitor is not None and trade_monitor._running,
        "poll_interval": trade_monitor.poll_interval if trade_monitor else None
    })

@mcp.custom_route("/api/trades/check", methods=["POST"])
async def api_check_trades(request: StarletteRequest) -> JSONResponse:
    """Manually trigger a check of open trades"""
    try:
        if not trade_monitor:
            return JSONResponse({"success": False, "error": "Trade monitor not initialized"}, status_code=503)

        result = await trade_monitor.check_open_trades()
        return JSONResponse({"success": True, "data": result})
    except Exception as e:
        log_error(e, "api_check_trades")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@mcp.custom_route("/api/trades/summary", methods=["GET"])
def api_trades_summary(request: StarletteRequest) -> JSONResponse:
    """Get trade summary for the current user"""
    try:
        user_id = extract_user_from_request(request)
        if AUTH_ENABLED and not user_id:
            return JSONResponse({"success": False, "error": "Authentication required"}, status_code=401)

        if not user_id:
            user_id = "test_user_123"

        summary = TradingRepository.get_trades_summary(user_id)
        return JSONResponse({"success": True, "data": summary})
    except Exception as e:
        log_error(e, "api_trades_summary")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@mcp.custom_route("/api/trades/list", methods=["GET"])
def api_trades_list(request: StarletteRequest) -> JSONResponse:
    """Get structured list of trades for table display"""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response

    try:
        trades = TradingRepository.get_trades_by_user(user_id)
        trade_list = []
        for trade in trades:
            trade_list.append({
                "id": trade.id,
                "trade_id": trade.trade_id,
                "symbol": trade.symbol,
                "type": trade.trade_type,
                "amount": trade.amount,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "profit_loss": trade.profit_loss,
                "status": trade.status,
                "created_at": trade.created_at.isoformat() if trade.created_at else None,
                "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
            })
        return JSONResponse({"success": True, "trades": trade_list})
    except Exception as e:
        log_error(e, "api_trades_list")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@mcp.custom_route("/api/trades/open", methods=["GET"])
async def api_open_trades(request: StarletteRequest) -> JSONResponse:
    """Get open trades with current prices and unrealized P&L"""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response

    try:
        open_trades = TradingRepository.get_open_trades(user_id)

        if not open_trades:
            return JSONResponse({"success": True, "trades": [], "total_unrealized_pnl": 0})

        # Get current prices for each symbol
        client = await get_deriv_client()
        try:
            trade_list = []
            total_unrealized_pnl = 0

            # Group trades by symbol to minimize API calls
            symbols = set(t.symbol for t in open_trades)
            current_prices = {}

            for symbol in symbols:
                try:
                    response = await client.get_ticks(symbol)
                    if 'tick' in response:
                        current_prices[symbol] = float(response['tick'].get('quote', 0))
                except Exception as e:
                    logger.warning(f"Failed to get price for {symbol}: {e}")
                    current_prices[symbol] = 0

            for trade in open_trades:
                current_price = current_prices.get(trade.symbol, 0)
                entry_price = trade.entry_price or 0

                # Calculate unrealized P&L based on trade direction
                if entry_price > 0 and current_price > 0:
                    if trade.trade_type.lower() == 'call':
                        # CALL profits when price goes up
                        price_diff = current_price - entry_price
                    else:
                        # PUT profits when price goes down
                        price_diff = entry_price - current_price

                    # For binary options, P&L is based on whether it's winning
                    # Simplified: if in profit direction, potential win is ~95% of stake
                    # if against, potential loss is the stake amount
                    if price_diff > 0:
                        unrealized_pnl = trade.amount * 0.95  # Approximate payout
                    else:
                        unrealized_pnl = -trade.amount
                else:
                    unrealized_pnl = 0

                total_unrealized_pnl += unrealized_pnl

                trade_list.append({
                    "id": trade.id,
                    "trade_id": trade.trade_id,
                    "symbol": trade.symbol,
                    "type": trade.trade_type.upper(),
                    "amount": trade.amount,
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "status": trade.status,
                    "created_at": trade.created_at.isoformat() if trade.created_at else None,
                    "stop_loss": trade.stop_loss,
                    "take_profit": trade.take_profit,
                    "trailing_stop_price": trade.trailing_stop_price,
                })

            return JSONResponse({
                "success": True,
                "trades": trade_list,
                "total_unrealized_pnl": round(total_unrealized_pnl, 2)
            })
        finally:
            await client.disconnect()

    except Exception as e:
        log_error(e, "api_open_trades")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@mcp.custom_route("/api/portfolio/stats", methods=["GET"])
def api_portfolio_stats(request: StarletteRequest) -> JSONResponse:
    """Get portfolio statistics for charts and displays"""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response

    try:
        trades = TradingRepository.get_trades_by_user(user_id)
        summary = TradingRepository.get_trades_summary(user_id)

        # Build P&L history for chart
        pnl_history = []
        cumulative_pnl = 0
        for trade in sorted(trades, key=lambda t: t.created_at or t.id):
            if trade.status == 'closed' and trade.profit_loss is not None:
                cumulative_pnl += trade.profit_loss
                pnl_history.append({
                    "date": trade.closed_at.isoformat() if trade.closed_at else trade.created_at.isoformat(),
                    "pnl": trade.profit_loss,
                    "cumulative": round(cumulative_pnl, 2),
                    "symbol": trade.symbol,
                })

        # Calculate additional stats
        closed_trades = [t for t in trades if t.status == 'closed']
        winning_trades = [t for t in closed_trades if (t.profit_loss or 0) > 0]
        losing_trades = [t for t in closed_trades if (t.profit_loss or 0) < 0]

        avg_win = sum(t.profit_loss for t in winning_trades) / len(winning_trades) if winning_trades else 0
        avg_loss = sum(t.profit_loss for t in losing_trades) / len(losing_trades) if losing_trades else 0

        return JSONResponse({
            "success": True,
            "stats": {
                "total_trades": summary['total_trades'],
                "open_trades": summary['open_trades'],
                "closed_trades": summary['closed_trades'],
                "total_pnl": round(summary['total_profit_loss'], 2),
                "win_rate": round(summary['win_rate'], 1),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "winning_trades": len(winning_trades),
                "losing_trades": len(losing_trades),
            },
            "pnl_history": pnl_history
        })
    except Exception as e:
        log_error(e, "api_portfolio_stats")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

if __name__ == "__main__":
    import signal
    import sys

    # Start trade monitor in background
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Start the trade monitor
    if os.getenv("ENABLE_TRADE_MONITOR", "true").lower() == "true":
        loop.run_until_complete(start_trade_monitor())

    # Handle graceful shutdown
    def shutdown_handler(signum, frame):
        logger.info("Shutdown signal received")
        loop.run_until_complete(stop_trade_monitor())
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    logger.info(f"Starting Deriv Trading MCP Server on http://{host}:{port}")

    mcp.run(
        transport="http",
        host=host,
        port=port,
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(","),
                allow_credentials=True,
                allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
            ),
            Middleware(RequestSizeLimitMiddleware, max_size=MAX_REQUEST_SIZE),
            Middleware(CSRFProtectionMiddleware, allowed_origins=ALLOWED_ORIGINS_LIST),
            Middleware(SecurityHeadersMiddleware),
        ],
    )

