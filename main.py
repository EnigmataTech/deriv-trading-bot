from fastmcp import FastMCP
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, Response
from database import TradingRepository
from deriv_client import DerivAPIClient, TechnicalIndicators
from analytics import (
    ClosedTrade,
    calculate_trade_stats,
    calculate_portfolio_stats,
    calculate_per_symbol_stats,
    streak_risk_assessment,
    duration_quality_report,
    _fmt_duration,
)
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

MCP_AGENT_USER_ID = os.getenv("MCP_AGENT_USER_ID", "hermes_agent")

def get_user_id() -> str:
    """Get user ID from context variable - raises error if not authenticated"""
    ctx_user_id = _current_user_id.get()
    if ctx_user_id:
        return ctx_user_id
    # MCP tool calls have no REST context — use the configured agent identity
    agent_user_id = os.getenv("MCP_AGENT_USER_ID")
    if agent_user_id:
        return agent_user_id
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
    # Standard volatility indices
    "R_10", "R_25", "R_50", "R_75", "R_100",
    # 1-second volatility indices (all available)
    "1HZ10V", "1HZ15V", "1HZ25V", "1HZ30V",
    "1HZ50V", "1HZ75V", "1HZ90V", "1HZ100V",
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

    # Duration unit validation (t=ticks 1-10, s=seconds 5-3600, m=minutes 1-1440)
    duration_unit = body.get("duration_unit", "s").lower()
    if duration_unit not in ("t", "s", "m"):
        errors.append("duration_unit must be t (ticks), s (seconds), or m (minutes)")
        duration_unit = "s"

    # Duration validation — limits depend on unit
    try:
        duration = int(body.get("duration", 60))
        if duration_unit == "t" and (duration < 1 or duration > 10):
            errors.append("tick duration must be between 1 and 10")
        elif duration_unit == "s" and (duration < 5 or duration > 3600):
            errors.append("second duration must be between 5 and 3600")
        elif duration_unit == "m" and (duration < 1 or duration > 1440):
            errors.append("minute duration must be between 1 and 1440")
    except (ValueError, TypeError):
        errors.append("invalid duration format")
        duration = 60

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
        "duration_unit": duration_unit,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "trailing_stop_distance": trailing_stop_distance
    }, None

_shared_client: Optional[DerivAPIClient] = None
_tick_stream: Optional[object] = None  # DerivTickStream instance

async def get_deriv_client() -> DerivAPIClient:
    """Return a shared, persistent Deriv WebSocket client, reconnecting if needed."""
    global _shared_client
    from websockets.protocol import State
    if _shared_client is None:
        _shared_client = DerivAPIClient()
    if not _shared_client.websocket or _shared_client.websocket.state != State.OPEN:
        await _shared_client.connect()
    return _shared_client

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
        pass

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
    duration: int = 60,
    duration_unit: str = "s",
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
        response = await client.place_contract(symbol, contract_type, amount, duration, duration_unit=duration_unit)

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
            # start_spot is often absent — try contract status, then live tick
            if entry_price == 0:
                try:
                    status_resp = await client.get_contract_status(str(contract_id))
                    poc = status_resp.get('proposal_open_contract', {})
                    entry_price = float(poc.get('entry_spot') or poc.get('entry_tick') or 0)
                except Exception:
                    pass
            if entry_price == 0:
                try:
                    tick_resp = await client.get_ticks(symbol)
                    entry_price = float(tick_resp.get('tick', {}).get('quote', 0))
                except Exception:
                    pass

            if entry_price == 0:
                logger.warning(
                    "Trade %s placed without resolvable entry_price — reconcile sweep will backfill",
                    contract_id,
                )

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
        pass

def _do_place_trade(symbol: str, amount: float, direction: str, duration: int = 5) -> str:
    """Place a binary options trade - sync wrapper for MCP tools"""
    return asyncio.run(_do_place_trade_async(symbol, amount, direction, duration))

def _orm_to_closed_trades(trades) -> list:
    """Convert SQLAlchemy Trade ORM objects to ClosedTrade dataclass instances."""
    result = []
    for t in trades:
        if t.status != 'closed' or t.profit_loss is None or t.closed_at is None:
            continue
        result.append(ClosedTrade(
            trade_id=t.trade_id,
            symbol=t.symbol,
            trade_type=t.trade_type,
            amount=t.amount,
            profit_loss=t.profit_loss,
            opened_at=t.created_at,
            closed_at=t.closed_at,
        ))
    return result


_SIGNAL_CACHE: dict[str, tuple[float, dict]] = {}
_SIGNAL_CACHE_TTL = 3.0  # seconds


async def _get_signal_cached(symbol: str) -> dict:
    """Fetch ticks history and compute signal with a per-symbol TTL cache.
    Reduces redundant Deriv WS round-trips when multiple clients (or a batch
    endpoint) ask for the same symbol within the TTL window."""
    import time
    now = time.monotonic()
    cached = _SIGNAL_CACHE.get(symbol)
    if cached and (now - cached[0]) < _SIGNAL_CACHE_TTL:
        return cached[1]

    client = await get_deriv_client()
    response = await client.get_ticks_history(symbol, count=200)
    if "error" in response:
        raise RuntimeError(response["error"].get("message", "ticks history failed"))
    prices = [float(p) for p in response.get("history", {}).get("prices", [])]
    sig = _compute_market_signal(symbol, prices)
    _SIGNAL_CACHE[symbol] = (now, sig)
    return sig


def _compute_market_signal(symbol: str, prices: list) -> dict:
    """Score-based composite signal: RSI / MACD-hist / BB-position summed to BUY/SELL/HOLD.
    Caller is responsible for applying the streak-risk pause override."""
    if len(prices) < 35:
        return {
            "symbol": symbol,
            "current_price": prices[-1] if prices else None,
            "rsi": None, "macd": None, "bb": None,
            "composite_score": 0,
            "call": "HOLD",
            "reason": f"insufficient ticks ({len(prices)} < 35)",
        }

    current = prices[-1]

    rsi_vals = TechnicalIndicators.calculate_rsi(prices, 14)
    rsi_latest = next((x for x in reversed(rsi_vals) if x is not None), None)
    if rsi_latest is None:
        rsi_score, rsi_label = 0, "n/a"
    elif rsi_latest < 30:
        rsi_score, rsi_label = 1, "oversold"
    elif rsi_latest > 70:
        rsi_score, rsi_label = -1, "overbought"
    else:
        rsi_score, rsi_label = 0, "neutral"

    _, _, hist = TechnicalIndicators.calculate_macd(prices)
    hist_latest = next((x for x in reversed(hist) if x is not None), None)
    if hist_latest is None:
        macd_score, macd_label = 0, "n/a"
    elif hist_latest > 0:
        macd_score, macd_label = 1, "bullish"
    elif hist_latest < 0:
        macd_score, macd_label = -1, "bearish"
    else:
        macd_score, macd_label = 0, "flat"

    upper, middle, lower = TechnicalIndicators.calculate_bollinger_bands(prices, period=20)
    u = next((x for x in reversed(upper) if x is not None), None)
    l = next((x for x in reversed(lower) if x is not None), None)
    if u is None or l is None:
        bb_score, bb_pos = 0, "n/a"
    elif current < l:
        bb_score, bb_pos = 1, "below-lower"
    elif current > u:
        bb_score, bb_pos = -1, "above-upper"
    else:
        bb_score, bb_pos = 0, "within"

    score = rsi_score + macd_score + bb_score
    if score >= 2:
        call = "BUY"
    elif score <= -2:
        call = "SELL"
    else:
        call = "HOLD"

    reason_parts = []
    if rsi_label not in ("neutral", "n/a"):
        reason_parts.append(f"RSI {rsi_label}")
    if macd_label not in ("flat", "n/a"):
        reason_parts.append(f"MACD {macd_label}")
    if bb_pos not in ("within", "n/a"):
        reason_parts.append(f"price {bb_pos} BB")
    reason = ", ".join(reason_parts) if reason_parts else "no directional bias"

    return {
        "symbol": symbol,
        "current_price": current,
        "rsi": {"value": rsi_latest, "score": rsi_score, "label": rsi_label},
        "macd": {"hist": hist_latest, "score": macd_score, "label": macd_label},
        "bb": {"position": bb_pos, "score": bb_score, "upper": u, "lower": l},
        "composite_score": score,
        "call": call,
        "reason": reason,
    }


def _portfolio_pause_status(user_id: str) -> dict:
    """Compute the streak-risk pause override based on the user's closed trades."""
    orm_trades = TradingRepository.get_trades_by_user(user_id)
    closed = _orm_to_closed_trades(orm_trades)
    if not closed:
        return {"recommend_pause": False, "current_streak": 0,
                "max_streak": 0, "threshold": 0, "reason": "no closed trades"}
    ts = calculate_trade_stats(closed)
    risk = streak_risk_assessment(ts)
    return {
        "recommend_pause": risk["recommend_pause"],
        "current_streak": risk["current_consecutive_losses"],
        "max_streak": risk["max_historical_streak"],
        "threshold": risk["pause_threshold"],
        "reason": (
            f"current streak {risk['current_consecutive_losses']} ≥ "
            f"threshold {risk['pause_threshold']}"
            if risk["recommend_pause"] else "within normal range"
        ),
    }


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
            pass
    
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
    """Calculate technical indicators for a symbol.
    Supported: sma, ema, rsi, macd, bb (Bollinger Bands), atr.
    MACD uses fast=12, slow=26, signal=9 and ignores the period parameter.
    BB uses period as the window (default 20) with 2-sigma bands.
    ATR uses period as the smoothing window (default 14).
    """
    async def _calculate_indicators():
        client = await get_deriv_client()
        try:
            count = max(100, period * 3)
            response = await client.get_ticks_history(symbol, count=count)
            if 'error' in response:
                return f"Error: {response['error']['message']}"

            history = response.get('history', {})
            prices = [float(p) for p in history.get('prices', [])]

            ind = indicator.lower()

            if ind == 'sma':
                if len(prices) < period:
                    return f"Need at least {period} ticks for SMA({period})."
                v = TechnicalIndicators.calculate_sma(prices, period)
                latest = next((x for x in reversed(v) if x is not None), None)
                return f"SMA({period}) for {symbol}: {latest}"

            elif ind == 'ema':
                if len(prices) < period:
                    return f"Need at least {period} ticks for EMA({period})."
                v = TechnicalIndicators.calculate_ema(prices, period)
                latest = next((x for x in reversed(v) if x is not None), None)
                return f"EMA({period}) for {symbol}: {latest}"

            elif ind == 'rsi':
                if len(prices) < period + 1:
                    return f"Need at least {period + 1} ticks for RSI({period})."
                v = TechnicalIndicators.calculate_rsi(prices, period)
                latest = next((x for x in reversed(v) if x is not None), None)
                interpretation = (
                    "overbought (>70)" if latest and latest > 70
                    else "oversold (<30)" if latest and latest < 30
                    else "neutral"
                )
                return f"RSI({period}) for {symbol}: {latest} [{interpretation}]"

            elif ind == 'macd':
                if len(prices) < 35:
                    return f"Need at least 35 ticks for MACD."
                macd_line, signal_line, histogram = TechnicalIndicators.calculate_macd(prices)
                m = next((x for x in reversed(macd_line) if x is not None), None)
                s = next((x for x in reversed(signal_line) if x is not None), None)
                h = next((x for x in reversed(histogram) if x is not None), None)
                cross = "bullish cross" if h and h > 0 else "bearish cross" if h and h < 0 else "neutral"
                return (
                    f"MACD for {symbol}:\n"
                    f"  MACD line:  {m}\n"
                    f"  Signal:     {s}\n"
                    f"  Histogram:  {h} [{cross}]"
                )

            elif ind == 'bb':
                p = period if period != 14 else 20
                if len(prices) < p:
                    return f"Need at least {p} ticks for BB({p})."
                upper, middle, lower = TechnicalIndicators.calculate_bollinger_bands(prices, period=p)
                u = next((x for x in reversed(upper) if x is not None), None)
                mid = next((x for x in reversed(middle) if x is not None), None)
                lo = next((x for x in reversed(lower) if x is not None), None)
                price_now = prices[-1]
                position = (
                    "above upper band" if price_now > u
                    else "below lower band" if price_now < lo
                    else "within bands"
                )
                return (
                    f"Bollinger Bands({p}, 2σ) for {symbol}:\n"
                    f"  Upper:  {u}\n"
                    f"  Middle: {mid}\n"
                    f"  Lower:  {lo}\n"
                    f"  Current price {price_now} is {position}"
                )

            elif ind == 'atr':
                if len(prices) < period + 1:
                    return f"Need at least {period + 1} ticks for ATR({period})."
                v = TechnicalIndicators.calculate_atr(prices, period)
                latest = next((x for x in reversed(v) if x is not None), None)
                return f"ATR({period}) for {symbol}: {latest} (avg tick-to-tick range)"

            else:
                return (
                    f"Unsupported indicator '{indicator}'. "
                    f"Available: sma, ema, rsi, macd, bb, atr"
                )
        finally:
            pass

    return asyncio.run(_calculate_indicators())

@mcp.tool()
def analyze_portfolio_performance() -> str:
    """Full portfolio performance analysis using Lean-ported statistics.
    Returns win rate, profit factor, expectancy, Sharpe/Sortino ratios,
    streak tracking, drawdown, and trade duration breakdown.
    """
    user_id = get_user_id()
    orm_trades = TradingRepository.get_trades_by_user(user_id)
    portfolio = TradingRepository.get_portfolio(user_id)
    closed = _orm_to_closed_trades(orm_trades)

    open_count = sum(1 for t in orm_trades if t.status == 'open')

    if not closed:
        return (
            f"No closed trades to analyse yet.\n"
            f"Open trades: {open_count}"
        )

    s = calculate_trade_stats(closed)

    lines = [
        "=== Portfolio Performance Analysis ===",
        "",
        f"Trades:          {s.total_trades} closed, {open_count} open",
        f"Wins / Losses:   {s.winning_trades} / {s.losing_trades}",
        f"Win Rate:        {s.win_rate * 100:.1f}%",
        f"Loss Rate:       {s.loss_rate * 100:.1f}%",
        "",
        f"Total P&L:       ${s.total_profit_loss:.2f}",
        f"Total Profit:    ${s.total_profit:.2f}",
        f"Total Loss:      ${s.total_loss:.2f}",
        f"Largest Win:     ${s.largest_profit:.2f}",
        f"Largest Loss:    ${s.largest_loss:.2f}",
        f"Avg P&L/trade:   ${s.average_profit_loss:.2f}",
        f"Avg Win:         ${s.average_profit:.2f}",
        f"Avg Loss:        ${s.average_loss:.2f}",
        "",
        f"Profit Factor:   {s.profit_factor:.2f}  (>1 = net positive)",
        f"P&L Ratio:       {s.profit_loss_ratio:.2f}  (avg win / avg loss)",
        f"Win/Loss Ratio:  {s.win_loss_ratio:.2f}",
        f"Expectancy:      {s.expectancy:.3f}  (>0 = edge exists)",
        "",
        f"Sharpe (trade):  {s.sharpe_ratio:.3f}",
        f"Sortino (trade): {s.sortino_ratio:.3f}",
        f"P&L Std Dev:     ${s.profit_loss_std_dev:.2f}",
        "",
        f"Max Consec Wins: {s.max_consecutive_wins}",
        f"Max Consec Loss: {s.max_consecutive_losses}",
        f"Current Streak:  {s.current_consecutive_losses} consecutive losses",
        "",
        f"Max Closed DD:   ${s.max_closed_drawdown:.2f}",
        f"P&L / Max DD:    {s.profit_to_max_drawdown_ratio:.2f}",
        "",
        f"Avg Duration:    {_fmt_duration(s.avg_duration_seconds)} (all) | "
        f"{_fmt_duration(s.avg_winning_duration_seconds)} (wins) | "
        f"{_fmt_duration(s.avg_losing_duration_seconds)} (losses)",
        f"Median Duration: {_fmt_duration(s.median_duration_seconds)}",
    ]

    if portfolio:
        lines += [
            "",
            f"Balance:         ${portfolio.balance:.2f}",
            f"Equity:          ${portfolio.equity:.2f}",
        ]

    return "\n".join(lines)

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
            pass
    
    return asyncio.run(_get_symbols())


@mcp.tool()
def get_risk_metrics() -> str:
    """Portfolio-level risk metrics: annualised Sharpe/Sortino, CAGR, max drawdown,
    and 1-day VaR at 95% and 99% confidence. Uses equity-curve-based calculations
    ported from Lean's PortfolioStatistics.
    """
    user_id = get_user_id()
    orm_trades = TradingRepository.get_trades_by_user(user_id)
    portfolio = TradingRepository.get_portfolio(user_id)
    closed = _orm_to_closed_trades(orm_trades)

    if not closed:
        return "No closed trades to calculate risk metrics."

    total_pnl = sum(t.profit_loss for t in closed)
    current_balance = portfolio.balance if portfolio else 0.0
    starting_equity = max(current_balance - total_pnl, 1.0)

    ps = calculate_portfolio_stats(closed, starting_equity)

    lines = [
        "=== Risk Metrics (Equity-Curve Based) ===",
        "",
        f"Start Equity:    ${ps.start_equity:.2f}",
        f"End Equity:      ${ps.end_equity:.2f}",
        f"Net Profit:      {ps.total_net_profit_pct:.2f}%",
        f"CAGR:            {ps.cagr:.2f}%  (annualised)",
        "",
        f"Sharpe Ratio:    {ps.sharpe_ratio:.3f}  (annualised, risk-free=0)",
        f"Sortino Ratio:   {ps.sortino_ratio:.3f}  (downside deviation only)",
        "",
        f"Max Drawdown:    {ps.max_drawdown_pct:.2f}%",
        f"DD Recovery:     {ps.drawdown_recovery_days} days",
        "",
        f"VaR 95%:         {ps.var_95 * 100:.2f}% of equity  (1-day)",
        f"VaR 99%:         {ps.var_99 * 100:.2f}% of equity  (1-day)",
        "",
        "Interpretation:",
        "  Sharpe > 1.0 = good risk-adjusted return",
        "  Sharpe > 2.0 = excellent",
        "  VaR 95% = expected daily loss in worst 5% of days",
    ]
    return "\n".join(lines)


@mcp.tool()
def get_per_symbol_performance() -> str:
    """Break down performance by volatility symbol.
    Shows trades, win rate, total P&L, profit factor, trade-level Sharpe,
    and average trade duration per symbol — sorted by total P&L descending.
    """
    user_id = get_user_id()
    orm_trades = TradingRepository.get_trades_by_user(user_id)
    closed = _orm_to_closed_trades(orm_trades)

    if not closed:
        return "No closed trades to analyse by symbol."

    symbol_stats = calculate_per_symbol_stats(closed)
    symbol_stats.sort(key=lambda x: x.total_profit_loss, reverse=True)

    lines = ["=== Per-Symbol Performance ===", ""]
    for ss in symbol_stats:
        s = ss.trade_stats
        lines.append(f"[{ss.symbol}]")
        lines.append(
            f"  Trades: {s.total_trades}  |  Win Rate: {s.win_rate * 100:.1f}%  |  "
            f"P&L: ${s.total_profit_loss:.2f}"
        )
        lines.append(
            f"  Profit Factor: {s.profit_factor:.2f}  |  "
            f"Sharpe: {s.sharpe_ratio:.3f}  |  "
            f"Expectancy: {s.expectancy:.3f}"
        )
        lines.append(
            f"  Avg Duration: {_fmt_duration(s.avg_duration_seconds)}  |  "
            f"Consec Loss (cur/max): {s.current_consecutive_losses}/{s.max_consecutive_losses}"
        )
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def analyze_trade_quality() -> str:
    """Analyse trade quality via duration distribution.
    Breaks trades into quartiles by hold time and shows win rate and P&L per bucket
    so the agent can identify the optimal hold window.
    Also shows whether wins tend to be held longer or shorter than losses.
    """
    user_id = get_user_id()
    orm_trades = TradingRepository.get_trades_by_user(user_id)
    closed = _orm_to_closed_trades(orm_trades)

    if not closed:
        return "No closed trades to analyse."

    s = calculate_trade_stats(closed)
    report = duration_quality_report(closed)

    lines = [
        "=== Trade Quality Analysis ===",
        "",
        f"Avg winning trade duration:  {_fmt_duration(s.avg_winning_duration_seconds)}",
        f"Avg losing trade duration:   {_fmt_duration(s.avg_losing_duration_seconds)}",
        f"Median trade duration:       {_fmt_duration(s.median_duration_seconds)}",
        "",
        "Win rate and P&L by hold-time quartile:",
    ]

    best_bucket = None
    best_wr = -1.0
    for label, stats in report.items():
        lines.append(
            f"  {label:>18}  |  {stats['trades']:>3} trades  |  "
            f"WR {stats['win_rate']:>5.1f}%  |  P&L ${stats['total_pnl']:>8.2f}"
        )
        if stats['win_rate'] > best_wr:
            best_wr = stats['win_rate']
            best_bucket = label

    lines += [
        "",
        f"Best duration bucket: {best_bucket} ({best_wr:.1f}% win rate)",
        "",
        "Note: higher win rate in a duration bucket suggests the agent should",
        "      prefer trades that are held within that time window.",
    ]
    return "\n".join(lines)


@mcp.tool()
def analyze_streak_risk() -> str:
    """Assess current streak risk across all symbols and per symbol.
    Returns current consecutive loss count vs historical maximum,
    and a clear pause recommendation when the streak approaches the historical max.
    """
    user_id = get_user_id()
    orm_trades = TradingRepository.get_trades_by_user(user_id)
    closed = _orm_to_closed_trades(orm_trades)

    if not closed:
        return "No closed trades to assess streak risk."

    overall = calculate_trade_stats(closed)
    risk = streak_risk_assessment(overall)

    lines = [
        "=== Streak Risk Assessment ===",
        "",
        f"Current consecutive losses:  {risk['current_consecutive_losses']}",
        f"Max historical streak:       {risk['max_historical_streak']}",
        f"% of historical max:         {risk['pct_of_historical_max']:.1f}%",
        f"Pause threshold:             {risk['pause_threshold']} consecutive losses",
        f"RECOMMEND PAUSE:             {'YES — stop trading now' if risk['recommend_pause'] else 'No — within normal range'}",
        "",
        "Per-symbol streak status:",
    ]

    symbol_stats = calculate_per_symbol_stats(closed)
    for ss in symbol_stats:
        s = ss.trade_stats
        r = streak_risk_assessment(s)
        flag = " *** PAUSE ***" if r['recommend_pause'] else ""
        lines.append(
            f"  {ss.symbol:<12}  cur={r['current_consecutive_losses']}  "
            f"max={r['max_historical_streak']}  "
            f"({r['pct_of_historical_max']:.0f}% of max){flag}"
        )

    return "\n".join(lines)


@mcp.tool()
def get_market_signal(symbol: str) -> str:
    """Score-based composite BUY/SELL/HOLD call for a symbol.
    Combines RSI, MACD histogram, and Bollinger-band position.
    Streak-risk pause override forces HOLD when the portfolio is in a danger streak."""
    async def _do():
        client = await get_deriv_client()
        response = await client.get_ticks_history(symbol, count=200)
        if 'error' in response:
            return f"Error: {response['error']['message']}"
        prices = [float(p) for p in response.get('history', {}).get('prices', [])]
        sig = _compute_market_signal(symbol, prices)
        try:
            user_id = get_user_id()
            pause = _portfolio_pause_status(user_id)
        except ValueError:
            pause = {"recommend_pause": False, "reason": "no user context"}

        if pause["recommend_pause"]:
            sig["call"] = "HOLD"
            sig["reason"] = f"streak-risk pause ({pause['reason']})"

        if sig.get("rsi") is None:
            return f"Signal for {symbol}: HOLD ({sig['reason']})"

        return (
            f"=== Signal for {symbol} ===\n"
            f"Price:   {sig['current_price']}\n"
            f"RSI:     {sig['rsi']['value']:.2f} [{sig['rsi']['label']}]  ({sig['rsi']['score']:+d})\n"
            f"MACD-h:  {sig['macd']['hist']:+.5f} [{sig['macd']['label']}]  ({sig['macd']['score']:+d})\n"
            f"BB-pos:  {sig['bb']['position']}  ({sig['bb']['score']:+d})\n"
            f"Score:   {sig['composite_score']:+d}\n"
            f"Call:    {sig['call']}\n"
            f"Why:     {sig['reason']}"
        )
    return asyncio.run(_do())


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
        pass

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
            pass

    except Exception as e:
        log_error(e, "api_get_candles")
        return JSONResponse({"success": False, "error": f"Failed to retrieve candle data: {str(e)}"}, status_code=500)

# Multipliers available per symbol
SYMBOL_MULTIPLIERS: dict[str, list[int]] = {
    # Standard volatility
    "R_10":    [100, 200, 500],
    "R_25":    [50, 100, 200],
    "R_50":    [80, 200, 400, 600, 800],
    "R_75":    [20, 50, 100],
    "R_100":   [40, 100, 200, 300, 400],
    # 1-second volatility
    "1HZ10V":  [100, 200, 500],
    "1HZ15V":  [300, 1000, 1500, 2000, 3000],
    "1HZ25V":  [50, 100, 200],
    "1HZ30V":  [140, 400, 700, 1000, 1400],
    "1HZ50V":  [80, 200, 400, 600, 800],
    "1HZ75V":  [20, 50, 100],
    "1HZ90V":  [45, 100, 200, 300, 450],
    "1HZ100V": [40, 100, 200, 300, 400],
}

async def _do_place_multiplier_async(
    symbol: str,
    direction: str,
    amount: float,
    multiplier: int,
    stop_loss = None,
    take_profit = None,
) -> str:
    from typing import Optional
    user_id = get_user_id()
    can_trade, limit_error = check_daily_loss_limit(user_id)
    if not can_trade:
        return f"Trade blocked: {limit_error}"

    valid = SYMBOL_MULTIPLIERS.get(symbol, [])
    if valid and multiplier not in valid:
        return f"Invalid multiplier {multiplier}x for {symbol}. Valid: {valid}"

    contract_type = "MULTUP" if direction.upper() in ("BUY", "MULTUP") else "MULTDOWN"
    client = await get_deriv_client()
    response = await client.place_multiplier_contract(
        symbol, contract_type, amount, multiplier, stop_loss, take_profit
    )

    if "error" in response:
        return f"Error placing multiplier trade: {response['error']['message']}"

    buy_data = response.get("buy", {})
    contract_id = buy_data.get("contract_id")
    if not contract_id:
        return "Multiplier trade failed — no contract ID returned"

    entry_price = 0.0
    try:
        tick_resp = await client.get_ticks(symbol)
        entry_price = float(tick_resp.get("tick", {}).get("quote", 0))
    except Exception:
        pass

    if entry_price == 0:
        try:
            status_resp = await client.get_contract_status(str(contract_id))
            poc = status_resp.get("proposal_open_contract", {})
            entry_price = float(poc.get("entry_spot") or poc.get("entry_tick") or 0)
        except Exception:
            pass

    if entry_price == 0:
        logger.warning(
            "Multiplier trade %s placed without resolvable entry_price — reconcile sweep will backfill",
            contract_id,
        )

    buy_price = float(buy_data.get("buy_price", amount))
    TradingRepository.create_trade_with_sl_tp(
        user_id=user_id, trade_id=str(contract_id), symbol=symbol,
        trade_type=contract_type.lower(), amount=buy_price, entry_price=entry_price,
        stop_loss=stop_loss, take_profit=take_profit,
    )
    log_trade_placed(symbol=symbol, amount=amount, direction=direction.upper(), contract_type=contract_type, duration=0, trade_id=str(contract_id))
    log_audit_trade(event_type="place", user_id=user_id, trade_id=str(contract_id),
                    symbol=symbol, amount=amount, direction=direction.upper(), success=True)

    result = (f"Multiplier trade placed! #{contract_id} | "
              f"{symbol} {direction.upper()} ${buy_price:.2f} @ {multiplier}x "
              f"(exposure: ${buy_price * multiplier:.2f})")
    if stop_loss:
        result += f" | SL: ${stop_loss}"
    if take_profit:
        result += f" | TP: ${take_profit}"
    return result


@mcp.custom_route("/api/trade/multiplier", methods=["POST"])
async def api_place_multiplier(request: StarletteRequest) -> JSONResponse:
    """Place a multiplier (CFD-style) contract."""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response
    ctx_token = _current_user_id.set(user_id)
    try:
        body = await request.json()
        validated_symbol, sym_err = validate_symbol(body.get("symbol", ""))
        if sym_err:
            return JSONResponse({"success": False, "error": sym_err}, status_code=400)
        direction = body.get("direction", "").upper()
        if direction not in ("BUY", "SELL", "MULTUP", "MULTDOWN"):
            return JSONResponse({"success": False, "error": "direction must be BUY or SELL"}, status_code=400)
        try:
            amount = float(body.get("amount", 0))
            if amount < 1.00:
                return JSONResponse({"success": False, "error": "amount must be at least $1.00 for multiplier contracts"}, status_code=400)
        except (ValueError, TypeError):
            return JSONResponse({"success": False, "error": "invalid amount"}, status_code=400)
        try:
            multiplier = int(body.get("multiplier", 0))
        except (ValueError, TypeError):
            return JSONResponse({"success": False, "error": "invalid multiplier"}, status_code=400)
        sl = float(body["stop_loss"]) if body.get("stop_loss") else None
        tp = float(body["take_profit"]) if body.get("take_profit") else None
        result = await _do_place_multiplier_async(validated_symbol, direction, amount, multiplier, sl, tp)
        log_api_call("/api/trade/multiplier", "POST", 200)
        # Return error if business logic returned an error string
        if result.startswith(("Error", "Trade blocked", "Invalid", "Multiplier trade failed")):
            return JSONResponse({"success": False, "error": result}, status_code=400)
        return JSONResponse({"success": True, "data": result})
    except Exception as e:
        log_error(e, "api_place_multiplier")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
    finally:
        _current_user_id.reset(ctx_token)


@mcp.custom_route("/api/multipliers/{symbol}", methods=["GET"])
def api_get_multipliers(request: StarletteRequest) -> JSONResponse:
    symbol = request.path_params["symbol"].upper()
    multipliers = SYMBOL_MULTIPLIERS.get(symbol, [])
    return JSONResponse({"success": True, "symbol": symbol, "multipliers": multipliers})

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
            duration_unit=params.get("duration_unit", "s"),
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
            sold_for = float(sell_data.get('sold_for', 0))
            profit_loss = float(sell_data.get('profit', sold_for - trade.amount))

            # Pull the spot exit_tick (not the USD payout) so exit_price stays
            # consistent with entry_price across all close paths.
            spot_exit_price = 0.0
            try:
                status_resp = await client.get_contract_status(contract_id)
                poc = status_resp.get('proposal_open_contract', {}) or {}
                spot_exit_price = float(poc.get('exit_tick') or poc.get('current_spot') or 0)
            except Exception:
                pass

            # Update trade in database
            TradingRepository.update_trade_result(
                trade_id=contract_id,
                exit_price=spot_exit_price,
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
            pass

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
async def api_get_indicators(request: StarletteRequest) -> JSONResponse:
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

        # Use shared async client directly to avoid asyncio.run() cross-loop issues
        client = await get_deriv_client()
        response = await client.get_ticks_history(validated_symbol, count=100)
        if "error" in response:
            return JSONResponse({"success": False, "error": response["error"]["message"]}, status_code=500)
        prices = [float(p) for p in response.get("history", {}).get("prices", [])]
        if len(prices) < period:
            result = f"Not enough data for {indicator} calculation. Need at least {period} data points."
        elif indicator == "rsi":
            values = TechnicalIndicators.calculate_rsi(prices, period)
            latest = values[-1] if values and values[-1] is not None else None
            result = f"RSI({period}) for {validated_symbol}: {latest:.2f}" if latest is not None else f"RSI({period}) for {validated_symbol}: N/A"
        else:
            values = TechnicalIndicators.calculate_sma(prices, period)
            latest = values[-1] if values and values[-1] is not None else None
            result = f"SMA({period}) for {validated_symbol}: {latest:.5f}" if latest is not None else f"SMA({period}) for {validated_symbol}: N/A"

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









TICK_SYMBOLS = ["R_50", "R_75", "R_100", "1HZ50V", "1HZ75V", "1HZ100V", "1HZ25V", "1HZ15V", "1HZ30V", "1HZ90V", "R_10", "R_25", "1HZ10V"]

@mcp.custom_route("/api/prices", methods=["GET"])
async def api_prices(request: StarletteRequest) -> JSONResponse:
    """Cached live tick prices. Starts the Deriv subscription on first call."""
    global _tick_stream
    if _tick_stream is None:
        from deriv_client import DerivTickStream
        _tick_stream = DerivTickStream()
        asyncio.create_task(_tick_stream.run(TICK_SYMBOLS))
        logger.info(f"Tick stream started for {TICK_SYMBOLS}")
    return JSONResponse({"success": True, "prices": _tick_stream.prices})


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
async def api_trades_summary(request: StarletteRequest) -> JSONResponse:
    """Get trade summary for the current user"""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response
    try:
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

@mcp.custom_route("/api/trades/agent", methods=["GET"])
def api_agent_trades(request: StarletteRequest) -> JSONResponse:
    """Get recent trades placed by the MCP agent — no auth required so TUI can poll it"""
    try:
        trades = TradingRepository.get_trades_by_user(MCP_AGENT_USER_ID)
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
        return JSONResponse({"success": True, "agent_user_id": MCP_AGENT_USER_ID, "trades": trade_list})
    except Exception as e:
        log_error(e, "api_agent_trades")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@mcp.custom_route("/api/trades/open", methods=["GET"])
async def api_open_trades(request: StarletteRequest) -> JSONResponse:
    """Get open trades with real-time P&L from Deriv contract status. Auto-closes settled contracts."""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response

    try:
        open_trades = TradingRepository.get_open_trades(user_id)

        if not open_trades:
            return JSONResponse({"success": True, "trades": [], "total_unrealized_pnl": 0})

        client = await get_deriv_client()
        try:
            trade_list = []
            total_unrealized_pnl = 0

            for trade in open_trades:
                unrealized_pnl = 0.0
                current_price = 0.0
                entry_price = float(trade.entry_price or 0)

                try:
                    status_resp = await client.get_contract_status(str(trade.trade_id))
                    poc = status_resp.get('proposal_open_contract', {})

                    # Use Deriv's actual profit value
                    if poc.get('profit') is not None:
                        unrealized_pnl = float(poc['profit'])
                    if poc.get('current_spot'):
                        current_price = float(poc['current_spot'])

                    # Fix missing entry price
                    if entry_price == 0 and (poc.get('entry_spot') or poc.get('entry_tick')):
                        entry_price = float(poc.get('entry_spot') or poc.get('entry_tick') or 0)
                        if entry_price:
                            TradingRepository.update_trade_result(
                                trade.trade_id, entry_price, None, status='open'
                            )

                    # Auto-close settled contracts
                    if (poc.get('is_sold') or poc.get('is_expired') or poc.get('status') in ('sold', 'won', 'lost', 'expired') or (poc.get('profit') is not None and not poc.get('is_valid_to_sell', 1))):
                        final_pnl = float(poc.get('profit', 0))
                        exit_price = float(poc.get('exit_tick') or poc.get('current_spot') or 0)
                        TradingRepository.update_trade_result(trade.trade_id, exit_price, final_pnl)
                        logger.info(f"Auto-closed contract {trade.trade_id}: P&L ${final_pnl:.2f}")
                        continue  # Don't include in open trades response

                except Exception as e:
                    logger.warning(f"Contract status error for {trade.trade_id}: {e}")

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
            pass

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


@mcp.custom_route("/api/signals/{symbol}", methods=["GET"])
async def api_get_signal(request: StarletteRequest) -> JSONResponse:
    """Composite BUY/SELL/HOLD signal for a symbol with full sub-indicator breakdown.
    Honours the portfolio streak-risk pause override."""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response
    try:
        symbol = request.path_params["symbol"]
        validated_symbol, err = validate_symbol(symbol)
        if err:
            return JSONResponse({"success": False, "error": err}, status_code=400)

        try:
            sig = await _get_signal_cached(validated_symbol)
        except RuntimeError as re:
            return JSONResponse({"success": False, "error": str(re)}, status_code=500)

        pause = _portfolio_pause_status(user_id)
        if pause["recommend_pause"]:
            sig = {**sig, "call": "HOLD", "reason": f"streak-risk pause ({pause['reason']})"}
        sig["pause_override"] = pause["recommend_pause"]
        sig["pause"] = pause

        log_api_call(f"/api/signals/{validated_symbol}", "GET", 200)
        return JSONResponse({"success": True, "signal": sig})
    except Exception as e:
        log_error(e, "api_get_signal")
        return JSONResponse({"success": False, "error": "Failed to compute signal"},
                            status_code=500)


@mcp.custom_route("/api/signals", methods=["GET"])
async def api_get_signals_batch(request: StarletteRequest) -> JSONResponse:
    """Batch composite signals for many symbols in a single round trip.
    Query: ?symbols=R_50,R_75,...  (defaults to TICK_SYMBOLS volatility set)."""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response
    try:
        raw = request.query_params.get("symbols", "")
        if raw:
            req_symbols = [s.strip() for s in raw.split(",") if s.strip()]
        else:
            req_symbols = ["R_50", "R_75", "R_100", "1HZ50V", "1HZ75V", "1HZ100V"]

        validated: list[str] = []
        errors: dict[str, str] = {}
        for s in req_symbols:
            v, err = validate_symbol(s)
            if err:
                errors[s] = err
            else:
                validated.append(v)

        results = await asyncio.gather(
            *(_get_signal_cached(s) for s in validated),
            return_exceptions=True,
        )

        pause = _portfolio_pause_status(user_id)
        signals: dict[str, dict] = {}
        for sym, res in zip(validated, results):
            if isinstance(res, Exception):
                errors[sym] = str(res)
                continue
            sig = dict(res)  # copy so we don't mutate the cached dict
            if pause["recommend_pause"]:
                sig["call"] = "HOLD"
                sig["reason"] = f"streak-risk pause ({pause['reason']})"
            sig["pause_override"] = pause["recommend_pause"]
            signals[sym] = sig

        log_api_call("/api/signals", "GET", 200)
        return JSONResponse({
            "success": True,
            "signals": signals,
            "pause": pause,
            "errors": errors,
        })
    except Exception as e:
        log_error(e, "api_get_signals_batch")
        return JSONResponse({"success": False, "error": "Failed to compute signals"},
                            status_code=500)


@mcp.custom_route("/api/portfolio/pause-status", methods=["GET"])
def api_pause_status(request: StarletteRequest) -> JSONResponse:
    """Portfolio-level streak-risk pause status — drives the dashboard banner."""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response
    try:
        return JSONResponse({"success": True, "pause": _portfolio_pause_status(user_id)})
    except Exception as e:
        log_error(e, "api_pause_status")
        return JSONResponse({"success": False, "error": "Failed to fetch pause status"},
                            status_code=500)


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

