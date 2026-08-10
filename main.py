from fastmcp import FastMCP
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, Response, HTMLResponse
from datetime import datetime
from database import TradingRepository
from indicators import TechnicalIndicators
from symbols import get_symbol_display_name
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
import statistics
from contextlib import asynccontextmanager
import time
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
@asynccontextmanager
async def _app_lifespan(server):
    """Start the trader's background tasks on the *server's* event loop.

    FastMCP runs this at ASGI app startup (the loop uvicorn actually serves on),
    so unlike scheduling them on a separately-created loop the trade monitor and
    balance-snapshot/outbox-flush loop genuinely run. Teardown is cancellation-
    shielded by FastMCP, so the outbox/monitor get a clean stop on SIGTERM.
    """
    bg_tasks = []
    # Only the trader bot (Deriv-token edge OR MT5 VPS) runs the monitor + the
    # balance-snapshot/outbox-flush loop. The token-less in-cluster read-only
    # backend skips both (no trades to reconcile; nothing to snapshot).
    is_trader = bool(os.getenv("DERIV_API_TOKEN")) or BROKER == "mt5"
    if is_trader and os.getenv("ENABLE_TRADE_MONITOR", "true").lower() == "true":
        try:
            await start_trade_monitor()
        except Exception as e:
            logger.warning("Trade monitor failed to start: %s", e)
    if is_trader:
        bg_tasks.append(asyncio.create_task(_balance_snapshot_loop()))
        # Deterministic no-LLM autotrader (self-gates on AUTOTRADE_ENABLED).
        bg_tasks.append(asyncio.create_task(_autotrade_loop()))
    try:
        yield
    finally:
        for t in bg_tasks:
            t.cancel()
        try:
            await stop_trade_monitor()
        except Exception as e:
            logger.warning("shutdown cleanup error: %s", e)


mcp = FastMCP(name="Deriv Trading MCP Server", lifespan=_app_lifespan)

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
    if MCP_AGENT_USER_ID:
        return MCP_AGENT_USER_ID
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
        logger.debug("Authentication disabled - using configured agent identity")
        return MCP_AGENT_USER_ID, None

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
    # Crash/Boom indices BLACKLISTED (0% WR, -$38.14 — spike strategy failed):
    # "CRASH1000", "BOOM1000" were removed 2026-06-16 after portfolio post-mortem.
    # These instruments don't respond to RSI/MACD/BB mean-reversion signals and
    # the structural spike-bias strategy produced 0-for-7 with -$38.14 in losses.
}

# Daily loss protection limits
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "100"))  # flat-$ fallback when no balance snapshot
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))  # 5% of balance/day (research-backed)
MAX_DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "0.20"))      # 20% equity-drawdown kill-switch
MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "50"))  # 50 trades per day


def check_daily_loss_limit(user_id: str) -> tuple[bool, Optional[str]]:
    """
    Check if user has exceeded daily loss limit.
    Returns (can_trade, error_message).
    """
    from datetime import datetime, timedelta
    from database import SessionLocal, Trade, Portfolio

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

        # Daily loss limit = MAX_DAILY_LOSS_PCT of current balance; fall back to
        # the flat-$ MAX_DAILY_LOSS when there's no balance snapshot yet.
        pf = db.query(Portfolio).filter(Portfolio.user_id == user_id).first()
        balance = pf.balance if pf else 0.0
        limit = balance * MAX_DAILY_LOSS_PCT if balance > 0 else MAX_DAILY_LOSS

        if total_loss >= limit:
            return False, f"Daily loss limit reached (${total_loss:.2f} ≥ ${limit:.2f})"

        return True, None
    finally:
        db.close()


def check_max_drawdown(user_id: str) -> tuple[bool, Optional[str]]:
    """Equity-drawdown kill-switch. Returns (ok_to_trade, message). Trips when
    current equity has fallen MAX_DRAWDOWN_PCT below the recorded peak equity
    (high-water mark maintained by the balance-snapshot loop)."""
    from database import SessionLocal, Portfolio
    if MAX_DRAWDOWN_PCT <= 0:
        return True, None
    try:
        db = SessionLocal()
    except Exception as e:
        logger.warning("drawdown check skipped (Postgres unavailable): %s", e)
        return True, None   # when data is unavailable, don't block trading
    try:
        pf = db.query(Portfolio).filter(Portfolio.user_id == user_id).first()
        if not pf or not pf.peak_equity or pf.peak_equity <= 0:
            return True, None
        dd = (pf.peak_equity - pf.equity) / pf.peak_equity
        if dd >= MAX_DRAWDOWN_PCT:
            return False, (f"Max drawdown {dd*100:.1f}% (peak ${pf.peak_equity:.2f} "
                           f"→ equity ${pf.equity:.2f})")
        return True, None
    except Exception as e:
        logger.warning("drawdown check error: %s", e)
        return True, None   # on DB read error, allow trading (monitor will halt on next cycle)
    finally:
        try:
            db.close()
        except Exception:
            pass


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

_mt5_client: Optional[object] = None  # MT5Client instance (BROKER=mt5)

# Active broker for the trading core. "mt5" = the VPS trader talking to the
# headless MT5 terminal via the mt5linux bridge; "deriv" = legacy Deriv WS API.
# The token-less in-cluster read-only backend leaves this at "deriv" (it never
# trades; it serves snapshots from Postgres).
BROKER = os.getenv("BROKER", "deriv").lower()

async def get_deriv_client():
    """Return the active broker client (shared/persistent), reconnecting if needed.

    BROKER=mt5 → MT5Client (the VPS trader). Its read methods return the same
    Deriv-style shapes the read endpoints already consume, so all market-data /
    balance call sites work unchanged; the Deriv-specific trade/contract calls
    are branched on BROKER at their call sites.
    """
    global _mt5_client
    if BROKER != "mt5":
        raise RuntimeError(
            "No live broker available: BROKER != 'mt5'. The legacy Deriv WS client "
            "was decommissioned in the MT5 migration; read-only deployments must "
            "serve from Postgres snapshots, not a live broker.")
    if _mt5_client is None:
        from mt5_client import MT5Client
        _mt5_client = MT5Client()
    return _mt5_client

# ============ Business Logic Functions (shared by MCP tools and REST endpoints) ============

async def _do_get_balance() -> str:
    """Get account balance.

    With DERIV_API_TOKEN: live from Deriv (and snapshot to Postgres portfolios).
    With BROKER=mt5 (VPS trader): live from the MT5 terminal (and snapshot too).
    Without a token / not MT5 (cluster read-only backend): serve the latest Postgres
    snapshot written by the trader — avoids fighting for the single broker session.
    """
    if not os.getenv("DERIV_API_TOKEN") and BROKER != "mt5":
        user_id = get_user_id()
        p = TradingRepository.get_portfolio(user_id)
        if p:
            return f"Balance: ${p.balance} USD"
        return "Balance: $0.00 USD (no snapshot yet)"
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
    if BROKER == "mt5":
        return "Error: binary trades unavailable under MT5 — use POST /api/trade/mt5 (lot + price SL/TP)"
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
_SIGNAL_CACHE_TTL = 15.0  # seconds — TUI polls signals every 3s; a longer TTL keeps
                          # most requests on cache so they don't serialize behind the
                          # single shared MT5 broker connection (which spikes latency).


def _cleanup_signal_cache():
    """Evict expired entries from _SIGNAL_CACHE to prevent unbounded growth."""
    now = time.monotonic()
    expired = [k for k, (ts, _) in _SIGNAL_CACHE.items() if (now - ts) > _SIGNAL_CACHE_TTL]
    for k in expired:
        del _SIGNAL_CACHE[k]


async def _get_signal_cached(symbol: str) -> dict:
    """Fetch ticks history and compute signal with a per-symbol TTL cache.
    Reduces redundant Deriv WS round-trips when multiple clients (or a batch
    endpoint) ask for the same symbol within the TTL window."""
    import time
    _cleanup_signal_cache()
    now = time.monotonic()
    cached = _SIGNAL_CACHE.get(symbol)
    if cached and (now - cached[0]) < _SIGNAL_CACHE_TTL:
        return cached[1]

    client = await get_deriv_client()
    # MT5 needs the display name ("Volatility 75 Index"), not the Deriv code
    # ("R_75"); without this the batch/signal endpoints get "no history" and the
    # TUI signals panel comes up empty (the autotrader already translates).
    broker_symbol = get_symbol_display_name(symbol) if BROKER == "mt5" else symbol
    response = await client.get_ticks_history(broker_symbol, count=200)
    if "error" in response:
        raise RuntimeError(response["error"].get("message", "ticks history failed"))
    prices = [float(p) for p in response.get("history", {}).get("prices", [])]
    sig = _compute_market_signal(symbol, prices)
    _SIGNAL_CACHE[symbol] = (now, sig)
    return sig


def _compute_market_signal(symbol: str, prices: list) -> dict:
    """Score-based composite signal: RSI / MACD-hist / BB-position summed to BUY/SELL/HOLD,
    gated by a 50-EMA trend filter (longs only above EMA — the one component every
    source in 2026-06-16 research agreed on). Also returns ATR-based suggested SL/TP
    (1.5x ATR stop, 2:1 reward) for the caller to attach to a live order.
    Caller is responsible for applying the streak-risk pause override."""
    if len(prices) < 55:
        return {
            "symbol": symbol,
            "current_price": prices[-1] if prices else None,
            "rsi": None, "macd": None, "bb": None, "ema50": None, "atr": None,
            "composite_score": 0,
            "call": "HOLD",
            "reason": f"insufficient bars ({len(prices)} < 55)",
            "suggested_sl": None, "suggested_tp": None,
        }

    current = prices[-1]

    rsi_vals = TechnicalIndicators.calculate_rsi(prices, 14)
    rsi_latest = next((x for x in reversed(rsi_vals) if x is not None), None)
    # Graduated mean-reversion weight: a genuine extreme scores ±2 so it can
    # outweigh the opposing MACD momentum reading; mild readings stay neutral.
    if rsi_latest is None:
        rsi_score, rsi_label = 0, "n/a"
    elif rsi_latest < 20:
        rsi_score, rsi_label = 2, "deeply oversold"
    elif rsi_latest < 30:
        rsi_score, rsi_label = 1, "oversold"
    elif rsi_latest > 80:
        rsi_score, rsi_label = -2, "deeply overbought"
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
    m = next((x for x in reversed(middle) if x is not None), None)
    l = next((x for x in reversed(lower) if x is not None), None)
    # Graduated by how far price penetrates beyond the band: a deep break past
    # half a band-width scores ±2 (strong reversion), a shallow break ±1.
    if u is None or l is None:
        bb_score, bb_pos = 0, "n/a"
    elif current < l:
        half = (m - l) if (m is not None and m > l) else None
        if half and (l - current) > 0.5 * half:
            bb_score, bb_pos = 2, "far below-lower"
        else:
            bb_score, bb_pos = 1, "below-lower"
    elif current > u:
        half = (u - m) if (m is not None and u > m) else None
        if half and (current - u) > 0.5 * half:
            bb_score, bb_pos = -2, "far above-upper"
        else:
            bb_score, bb_pos = -1, "above-upper"
    else:
        bb_score, bb_pos = 0, "within"

    ema_vals = TechnicalIndicators.calculate_ema(prices, 50)
    ema50 = next((x for x in reversed(ema_vals) if x is not None), None)

    atr_vals = TechnicalIndicators.calculate_atr(prices, 14)
    atr = next((x for x in reversed(atr_vals) if x is not None), None)

    score = rsi_score + macd_score + bb_score
    if score >= 2:
        call = "BUY"
    elif score <= -2:
        call = "SELL"
    else:
        call = "HOLD"

    trend_note = None
    # 50-EMA trend filter — only long above the EMA. The single component
    # every source in the 2026-06-16 research converged on; applied to BUY
    # only since the bot is longs-only elsewhere too.
    if call == "BUY" and ema50 is not None and current <= ema50:
        call = "HOLD"
        trend_note = f"BUY blocked by trend filter — price {current:.2f} below 50-EMA {ema50:.2f}"

    suggested_sl = suggested_tp = None
    if call == "BUY" and atr is not None and atr > 0:
        stop_dist = 1.5 * atr
        suggested_sl = round(current - stop_dist, 5)
        suggested_tp = round(current + stop_dist * 2, 5)  # 2:1 reward:risk

    reason_parts = []
    if rsi_label not in ("neutral", "n/a"):
        reason_parts.append(f"RSI {rsi_label}")
    if macd_label not in ("flat", "n/a"):
        reason_parts.append(f"MACD {macd_label}")
    if bb_pos not in ("within", "n/a"):
        reason_parts.append(f"price {bb_pos} BB")
    reason = ", ".join(reason_parts) if reason_parts else "no directional bias"
    if trend_note:
        reason = trend_note

    return {
        "symbol": symbol,
        "current_price": current,
        "rsi": {"value": rsi_latest, "score": rsi_score, "label": rsi_label},
        "macd": {"hist": hist_latest, "score": macd_score, "label": macd_label},
        "bb": {"position": bb_pos, "score": bb_score, "upper": u, "lower": l},
        "ema50": ema50,
        "atr": atr,
        "composite_score": score,
        "call": call,
        "reason": reason,
        "suggested_sl": suggested_sl,
        "suggested_tp": suggested_tp,
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
async def get_account_balance() -> str:
    """Get current account balance from Deriv"""
    return await _do_get_balance()

@mcp.tool()
async def get_market_data(symbol: str) -> str:
    """Get real-time market data for a trading symbol"""
    client = await get_deriv_client()
    try:
        response = await client.get_ticks(symbol)
        if 'error' in response:
            return f"Error: {response['error']['message']}"
        tick_data = response.get('tick', {})
        return f"Symbol: {symbol}\nPrice: {tick_data.get('quote', 'N/A')}\nTime: {tick_data.get('epoch', 'N/A')}"
    finally:
        pass

@mcp.tool()
async def place_trade(symbol: str, amount: float, direction: str, duration: int = 5) -> str:
    """Place a binary options trade. Direction: 'CALL' or 'PUT'. (Unavailable under
    MT5 — use place_mt5_trade instead.)"""
    return await _do_place_trade_async(symbol, amount, direction, duration)

@mcp.tool()
async def get_symbol_specs(symbol: str) -> str:
    """Volume limits for a symbol so you can size lots correctly (min lot varies:
    e.g. Volatility 75=0.01, Volatility 75 (1s)=0.05, Volatility 100=1.0).
    Accepts MT5 names or legacy codes (R_75). MT5 only."""
    if BROKER != "mt5":
        return "Error: get_symbol_specs requires BROKER=mt5"
    name = get_symbol_display_name(symbol)
    client = await get_deriv_client()
    s = await client.get_symbol_specs(name)
    if s.get("error"):
        return f"Error: {s['error'].get('message')}"
    return (f"{name}: min_lot={s.get('volume_min')} step={s.get('volume_step')} "
            f"max_lot={s.get('volume_max')} trade_allowed={s.get('trade_allowed')}")

@mcp.tool()
async def place_mt5_trade(
    symbol: str,
    side: str,
    lot: float,
    stop_loss: float = 0.0,
    take_profit: float = 0.0,
    reason: str = "",
) -> str:
    """Place an MT5 trade (lot-sized, price-level SL/TP).

    Args:
        symbol: MT5 name ("Volatility 75 Index") or legacy code ("R_75").
        side: "buy" or "sell" (CALL/MULTUP map to buy; PUT/MULTDOWN to sell).
        lot: Lot size, e.g. 0.01 (min lot varies per symbol).
        stop_loss: Optional stop-loss *price* level (0 = none).
        take_profit: Optional take-profit *price* level (0 = none).
        reason: Short rationale for the trade — stored with it and shown in the TUI.
    """
    if BROKER != "mt5":
        return "Error: place_mt5_trade requires BROKER=mt5"
    s = side.strip().lower()
    if s in ("call", "multup", "buy"):
        s = "buy"
    elif s in ("put", "multdown", "sell"):
        s = "sell"
    else:
        return "Error: side must be buy/sell (or CALL/PUT)"
    return await _do_place_trade_mt5(
        symbol, s, lot,
        sl_price=stop_loss or None,
        tp_price=take_profit or None,
        reason=reason or None,
    )

@mcp.tool()
def record_agent_activity(
    symbol: str = "",
    score: float = None,
    decision: str = "",
    detail: str = "",
    event_type: str = "scan",
    trade_id: str = "",
) -> str:
    """Log one per-scan observability record so the agent's reasoning is visible
    in the TUI even when no trade is placed. Call this once per symbol evaluated
    (including SILENT/no-trade ones), so silent scans leave a record too.

    Args:
        symbol: Symbol evaluated, e.g. "R_75" or "Volatility 75 Index".
        score: Composite signal score at scan time (e.g. 1, -2).
        decision: What was decided — BUY, SELL, SILENT, or HOLD.
        detail: One-line human-readable rationale.
        event_type: "scan" (default), "trade", "error", or "heartbeat".
        trade_id: Trade id/ticket when this record corresponds to a placed trade.
    """
    try:
        sc = float(score) if score is not None else None
    except (ValueError, TypeError):
        sc = None
    row = TradingRepository.record_agent_activity(
        event_type=(event_type or "scan"),
        agent=MCP_AGENT_USER_ID,
        symbol=(symbol or None), score=sc,
        decision=(decision or None), detail=(detail or None),
        trade_id=(trade_id or None),
    )
    return f"Recorded activity #{row.id}" if row else "Activity dropped (db unavailable)"

@mcp.tool()
def get_trade_history() -> str:
    """Get trading history for the current user"""
    return _do_get_trade_history()

@mcp.tool()
async def calculate_technical_indicators(symbol: str, indicator: str, period: int = 14) -> str:
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

    return await _calculate_indicators()

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
async def get_active_symbols() -> str:
    """Get list of active trading symbols"""
    client = await get_deriv_client()
    try:
        response = await client.get_active_symbols()
        if 'error' in response:
            return f"Error: {response['error']['message']}"

        symbols = response.get('active_symbols', [])
        if not symbols:
            return "No active symbols found"

        result = "Active Trading Symbols:\n"
        for symbol in symbols[:20]:
            result += f"{symbol.get('symbol', 'N/A')} - {symbol.get('display_name', 'N/A')}\n"

        if len(symbols) > 20:
            result += f"... and {len(symbols) - 20} more symbols\n"

        return result
    finally:
        pass


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


def _signal_edge_report() -> str:
    """Win rate + expectancy bucketed by the autotrader's entry signal, so the
    data shows whether the signal actually beats a random baseline."""
    import re
    from collections import defaultdict
    user_id = get_user_id()
    trades = [t for t in TradingRepository.get_trades_by_user(user_id)
              if t.status == "closed" and t.profit_loss is not None]
    if not trades:
        return "No closed trades to analyse yet — let the autotrader gather some."

    def is_spike(t):
        return bool(t.reason and "spike" in t.reason.lower())

    def bucket(t):
        m = re.search(r"score\s*([+-]?\d+)", t.reason or "")
        if m:
            return f"score {int(m.group(1)):+d}"
        return "spike" if is_spike(t) else "manual/other"

    def stats(pnls):
        n = len(pnls)
        wins = [p for p in pnls if p > 0]
        gl = -sum(p for p in pnls if p <= 0)
        wr = len(wins) / n * 100 if n else 0.0
        exp = sum(pnls) / n if n else 0.0
        pf = (sum(wins) / gl) if gl > 0 else float("inf")
        return n, wr, exp, sum(pnls), pf

    groups = defaultdict(list)
    for t in trades:
        groups[bucket(t)].append(float(t.profit_loss))

    lines = ["=== Signal Edge Analysis ===",
             "(expectancy = avg P&L per trade; >0 after spread ⇒ real edge)", ""]
    n, wr, exp, tot, pf = stats([float(t.profit_loss) for t in trades])
    lines.append(f"ALL: {n} trades | win {wr:.1f}% | exp ${exp:+.3f} | total ${tot:+.2f} | PF {pf:.2f}")

    def order(k):
        m = re.search(r"([+-]?\d+)", k)
        return int(m.group(1)) if m else 999
    lines += ["", "By entry signal:"]
    for k in sorted(groups, key=order):
        n, wr, exp, tot, pf = stats(groups[k])
        lines.append(f"  {k:>12}: {n:>3} | win {wr:5.1f}% | exp ${exp:+.3f} | tot ${tot:+.2f} | PF {pf:.2f}")

    spike = [float(t.profit_loss) for t in trades if is_spike(t)]
    vol = [float(t.profit_loss) for t in trades if not is_spike(t)]
    lines.append("")
    if spike:
        n, wr, exp, tot, pf = stats(spike)
        lines.append(f"Crash/Boom (spike): {n} | win {wr:.1f}% | exp ${exp:+.3f} | PF {pf:.2f}")
    if vol:
        n, wr, exp, tot, pf = stats(vol)
        lines.append(f"Volatility (signal): {n} | win {wr:.1f}% | exp ${exp:+.3f} | PF {pf:.2f}")
    lines += ["",
              "Read: if expectancy climbs with |score|, the signal has edge. If it's flat or",
              "negative across buckets, entries are ~random (the expected result on RNG vol",
              "indices) — profit then depends on finding a structurally biased instrument."]
    return "\n".join(lines)


@mcp.tool()
def analyze_signal_edge() -> str:
    """Measure whether the entry signal has real predictive edge: win rate and
    expectancy bucketed by the autotrader's composite score (and Crash/Boom spike
    entries), versus a random baseline. Use this to decide what to scale."""
    return _signal_edge_report()


@mcp.custom_route("/api/analysis/edge", methods=["GET"])
def api_analysis_edge(request: StarletteRequest) -> JSONResponse:
    """No-auth edge report (the TUI reads this). Scoped to the agent's trades."""
    try:
        return JSONResponse({"success": True, "report": _signal_edge_report()})
    except Exception as e:
        log_error(e, "api_analysis_edge")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


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
async def get_market_signal(symbol: str) -> str:
    """Score-based composite BUY/SELL/HOLD call for a symbol.
    Combines RSI, MACD histogram, and Bollinger-band position.
    Streak-risk pause override forces HOLD when the portfolio is in a danger streak.
    Accepts MT5 names or legacy Deriv codes (R_75) under BROKER=mt5."""
    if BROKER == "mt5":
        symbol = get_symbol_display_name(symbol)  # R_75 -> "Volatility 75 Index"
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
        sig["suggested_sl"] = sig["suggested_tp"] = None

    if sig.get("rsi") is None:
        return f"Signal for {symbol}: HOLD ({sig['reason']})"

    trend_line = ""
    if sig.get("ema50") is not None:
        trend_line = f"50-EMA:  {sig['ema50']:.5f}  (price {'above' if sig['current_price'] > sig['ema50'] else 'at/below'})\n"

    sl_tp_line = ""
    if sig["call"] == "BUY" and sig.get("suggested_sl") is not None:
        sl_tp_line = (
            f"ATR(14): {sig['atr']:.5f}\n"
            f"Suggested SL: {sig['suggested_sl']}   Suggested TP: {sig['suggested_tp']}"
            f"  (1.5x ATR stop, 2:1 reward — pass these to the live order, don't invent your own)\n"
        )

    return (
        f"=== Signal for {symbol} ===\n"
        f"Price:   {sig['current_price']}\n"
        f"RSI:     {sig['rsi']['value']:.2f} [{sig['rsi']['label']}]  ({sig['rsi']['score']:+d})\n"
        f"MACD-h:  {sig['macd']['hist']:+.5f} [{sig['macd']['label']}]  ({sig['macd']['score']:+d})\n"
        f"BB-pos:  {sig['bb']['position']}  ({sig['bb']['score']:+d})\n"
        f"{trend_line}"
        f"Score:   {sig['composite_score']:+d}\n"
        f"Call:    {sig['call']}\n"
        f"{sl_tp_line}"
        f"Why:     {sig['reason']}"
    )


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

@mcp.custom_route("/api/account/mode", methods=["GET"])
async def api_get_account_mode(request: StarletteRequest) -> JSONResponse:
    """Report which account the bot is actually connected to, verified from MT5
    itself (not just config). Lets the TUI show DEMO vs LIVE unambiguously."""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response
    try:
        client = await get_deriv_client()
        # MT5 client carries verified account identity; Deriv path does not.
        trade_mode = getattr(client, "trade_mode", None)
        is_real = trade_mode == 2
        data = {
            "broker": BROKER,
            "mode": getattr(client, "account_mode", "demo"),
            "trade_mode": trade_mode,           # 0=demo, 1=contest, 2=real, None=n/a
            "is_real_money": bool(is_real),
            "login": getattr(client, "login", None),
            "server": getattr(client, "server", None),
            "currency": getattr(client, "currency", None),
        }
        log_api_call("/api/account/mode", "GET", 200)
        return JSONResponse({"success": True, "data": data})
    except Exception as e:
        log_error(e, "api_get_account_mode")
        return JSONResponse({"success": False, "error": "Failed to retrieve account mode"},
                            status_code=500)


_MARKET_CACHE: dict[str, tuple[float, str]] = {}
_MARKET_CACHE_TTL = 4.0  # seconds — the TUI market panel polls 6 symbols every 5s;
                         # cache so those don't each hit the broker every cycle.


async def _do_get_market_data(symbol: str) -> str:
    """Get market data - async business logic (short-TTL cached per symbol)."""
    import time
    now = time.monotonic()
    cached = _MARKET_CACHE.get(symbol)
    if cached and (now - cached[0]) < _MARKET_CACHE_TTL:
        return cached[1]
    client = await get_deriv_client()
    response = await client.get_ticks(symbol)
    if 'error' in response:
        return f"Error: {response['error']['message']}"
    tick_data = response.get('tick', {})
    result = f"Symbol: {symbol}\nPrice: {tick_data.get('quote', 'N/A')}\nTime: {tick_data.get('epoch', 'N/A')}"
    _MARKET_CACHE[symbol] = (now, result)
    return result

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

        broker_symbol = get_symbol_display_name(validated_symbol) if BROKER == "mt5" else validated_symbol
        result = await _do_get_market_data(broker_symbol)
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
        broker_symbol = get_symbol_display_name(validated_symbol) if BROKER == "mt5" else validated_symbol
        try:
            response = await client.get_candles(broker_symbol, granularity, count)

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

# Live multiplier ranges fetched from Deriv contracts_for (the static table
# above is out of sync for several symbols). Cached per symbol; falls back to
# the static table if the live fetch is unavailable.
_live_multiplier_cache: dict[str, list[int]] = {}

async def get_live_multipliers(symbol: str) -> list[int]:
    """Valid multipliers for a symbol per Deriv, cached; static table as fallback."""
    if symbol in _live_multiplier_cache:
        return _live_multiplier_cache[symbol]
    try:
        client = await get_deriv_client()
        resp = await client.get_contracts_for(symbol)
        mults: set[int] = set()
        for c in resp.get("contracts_for", {}).get("available", []):
            cat = (c.get("contract_category") or "").lower()
            ctype = (c.get("contract_type") or "").lower()
            if cat == "multiplier" or ctype in ("multup", "multdown"):
                for m in (c.get("multiplier_range") or c.get("multipliers") or []):
                    try:
                        mults.add(int(m))
                    except (ValueError, TypeError):
                        pass
        result = sorted(mults)
        if result:
            _live_multiplier_cache[symbol] = result
            return result
    except Exception as e:
        log_error(e, "get_live_multipliers")
    return SYMBOL_MULTIPLIERS.get(symbol, [])

async def _do_place_multiplier_async(
    symbol: str,
    direction: str,
    amount: float,
    multiplier: int,
    stop_loss = None,
    take_profit = None,
) -> str:
    from typing import Optional
    if BROKER == "mt5":
        # MT5 has no Deriv multiplier contracts; map to an MT5-native order
        # (amount→lot, SL/TP as price levels). multiplier arg is ignored.
        side = "buy" if direction.upper() in ("BUY", "MULTUP") else "sell"
        return await _do_place_trade_mt5(symbol, side, amount, stop_loss, take_profit)
    user_id = get_user_id()
    can_trade, limit_error = check_daily_loss_limit(user_id)
    if not can_trade:
        return f"Trade blocked: {limit_error}"

    valid = await get_live_multipliers(symbol)
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


async def _do_place_trade_mt5(
    symbol: str,
    side: str,
    lot: float,
    sl_price: Optional[float] = None,
    tp_price: Optional[float] = None,
    entry_price: Optional[float] = None,
    reason: Optional[str] = None,
    sl_dist: Optional[float] = None,
    tp_dist: Optional[float] = None,
) -> str:
    """Place an MT5-native order: lot size + price-level SL/TP (+ optional pending
    entry). Persists trade_id=str(ticket) and the typed mt5_ticket so the monitor
    can reconcile against positions_get(). Accepts either MT5 names ("Volatility
    75 Index") or legacy Deriv codes ("R_75"), and an optional agent rationale."""
    symbol = get_symbol_display_name(symbol)  # R_75 -> "Volatility 75 Index"; passthrough otherwise
    user_id = get_user_id()
    can_trade, limit_error = check_daily_loss_limit(user_id)
    if not can_trade:
        log_audit_trade(event_type="blocked", user_id=user_id, symbol=symbol,
                        amount=lot, direction=side, success=False, reason=limit_error)
        return f"Trade blocked: {limit_error}"

    side = side.lower()
    if side not in ("buy", "sell"):
        return "Error: side must be 'buy' or 'sell'"

    client = await get_deriv_client()  # MT5Client when BROKER=mt5
    # Validate lot against the symbol's volume constraints so the agent gets a
    # clear message instead of MT5's bare "Invalid volume" (min lot varies:
    # Vol75=0.01, Vol75(1s)=0.05, Vol100=1.0).
    specs = await client.get_symbol_specs(symbol)
    vmin = specs.get("volume_min") or 0
    if vmin and lot < vmin:
        return (f"Error: lot {lot} below min {vmin} for {symbol} "
                f"(step {specs.get('volume_step')}, max {specs.get('volume_max')})")
    # Distance-based SL/TP: place the market order naked (never rejected for
    # bad stops) and attach SL/TP from the real fill afterwards. Absolute
    # sl_price/tp_price (manual/agent path) keep the inline behavior.
    use_dist = sl_dist is not None or tp_dist is not None
    res = await client.place_order(
        symbol, side, lot,
        sl_price=None if use_dist else sl_price,
        tp_price=None if use_dist else tp_price,
        entry_price=entry_price)
    if not res.get("success"):
        err = res.get("error") or res.get("comment") or f"retcode {res.get('retcode')}"
        log_error(Exception(str(err)), "place_trade_mt5", error_code="MT5_ORDER_ERROR")
        log_audit_trade(event_type="place", user_id=user_id, symbol=symbol,
                        amount=lot, direction=side, success=False, reason=str(err))
        return f"Error placing MT5 trade: {err}"

    ticket = res.get("ticket")
    fill = float(res.get("price") or 0)
    if fill == 0:
        try:
            tick_resp = await client.get_ticks(symbol)
            fill = float(tick_resp.get("tick", {}).get("quote", 0))
        except Exception:
            pass

    # Attach SL/TP computed from the *actual* fill price (robust for Crash/Boom).
    if use_dist and fill and ticket:
        digits = int(specs.get("digits", 2))
        if side == "buy":
            sl_price = round(fill - sl_dist, digits) if sl_dist else None
            tp_price = round(fill + tp_dist, digits) if tp_dist else None
        else:
            sl_price = round(fill + sl_dist, digits) if sl_dist else None
            tp_price = round(fill - tp_dist, digits) if tp_dist else None
        sltp = await client.set_position_sltp(ticket, sl_price, tp_price)
        if not sltp.get("success"):
            logger.warning("SL/TP modify failed for #%s: %s — position left open without stops",
                           ticket, sltp.get("comment") or sltp.get("error"))
            sl_price = tp_price = None  # persist reality

    TradingRepository.create_trade_with_sl_tp(
        user_id=user_id, trade_id=str(ticket), symbol=symbol,
        trade_type=side, amount=float(lot), entry_price=fill,
        stop_loss=sl_price, take_profit=tp_price, mt5_ticket=int(ticket) if ticket else None,
        reason=reason,
    )
    log_trade_placed(symbol=symbol, amount=lot, direction=side.upper(),
                     contract_type="MT5", duration=0, trade_id=str(ticket))
    log_audit_trade(event_type="place", user_id=user_id, trade_id=str(ticket),
                    symbol=symbol, amount=lot, direction=side.upper(), success=True)

    kind = "pending" if entry_price else "market"
    result = f"MT5 trade placed! #{ticket} | {symbol} {side.upper()} {lot} lot ({kind}) @ {fill}"
    if sl_price:
        result += f" | SL: {sl_price}"
    if tp_price:
        result += f" | TP: {tp_price}"
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
            # Under MT5 the "amount" is interpreted as lot size (e.g. 0.01); the
            # $1.00 floor only applies to Deriv multiplier contracts.
            if BROKER != "mt5" and amount < 1.00:
                return JSONResponse({"success": False, "error": "amount must be at least $1.00 for multiplier contracts"}, status_code=400)
            if amount <= 0:
                return JSONResponse({"success": False, "error": "invalid amount"}, status_code=400)
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


@mcp.custom_route("/api/trade/mt5", methods=["POST"])
async def api_place_trade_mt5(request: StarletteRequest) -> JSONResponse:
    """Place an MT5-native order: {symbol, side: buy|sell, lot, stop_loss?,
    take_profit?, entry_price?} where SL/TP/entry are price levels."""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response
    ctx_token = _current_user_id.set(user_id)
    try:
        body = await request.json()
        validated_symbol, sym_err = validate_symbol(body.get("symbol", ""))
        if sym_err:
            return JSONResponse({"success": False, "error": sym_err}, status_code=400)
        side = str(body.get("side", body.get("direction", ""))).lower()
        if side in ("call", "multup"):
            side = "buy"
        elif side in ("put", "multdown"):
            side = "sell"
        if side not in ("buy", "sell"):
            return JSONResponse({"success": False, "error": "side must be buy or sell"}, status_code=400)
        try:
            lot = float(body.get("lot", body.get("amount", 0)))
            if lot <= 0:
                return JSONResponse({"success": False, "error": "lot must be > 0"}, status_code=400)
        except (ValueError, TypeError):
            return JSONResponse({"success": False, "error": "invalid lot"}, status_code=400)
        sl = float(body["stop_loss"]) if body.get("stop_loss") else None
        tp = float(body["take_profit"]) if body.get("take_profit") else None
        entry = float(body["entry_price"]) if body.get("entry_price") else None
        reason = (str(body.get("reason")).strip() or None) if body.get("reason") else None
        result = await _do_place_trade_mt5(validated_symbol, side, lot, sl, tp, entry, reason=reason)
        log_api_call("/api/trade/mt5", "POST", 200)
        if result.startswith(("Error", "Trade blocked")):
            return JSONResponse({"success": False, "error": result}, status_code=400)
        return JSONResponse({"success": True, "data": result})
    except Exception as e:
        log_error(e, "api_place_trade_mt5")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
    finally:
        _current_user_id.reset(ctx_token)


@mcp.custom_route("/api/multipliers/{symbol}", methods=["GET"])
async def api_get_multipliers(request: StarletteRequest) -> JSONResponse:
    symbol = request.path_params["symbol"].upper()
    multipliers = await get_live_multipliers(symbol)
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

        # Close the position (MT5) or sell the contract (Deriv)
        client = await get_deriv_client()
        if BROKER == "mt5":
            ticket = int(trade.mt5_ticket or contract_id)
            res = await client.close_position(ticket)
            if not res.get("success"):
                err = res.get("error") or res.get("comment") or f"retcode {res.get('retcode')}"
                return JSONResponse({"success": False, "error": str(err)}, status_code=500)
            close = await client.get_position_close(ticket)
            profit_loss = float(close.get("profit") or 0.0)
            TradingRepository.update_trade_result(
                trade_id=contract_id,
                exit_price=float(close.get("exit_price") or 0.0),
                profit_loss=profit_loss,
                status='closed',
            )
            log_api_call(f"/api/trade/{contract_id}/sell", "POST", 200)
            return JSONResponse({"success": True, "data": {
                "contract_id": contract_id, "ticket": ticket, "profit_loss": profit_loss}})
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
    """Live tick prices. The Deriv tick-stream subscription was decommissioned in
    the MT5 migration; this will be served from the VPS→Postgres price snapshot
    (Phase C). Returns an empty map for now — the TUI handles that gracefully."""
    return JSONResponse({"success": True, "prices": {}})


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
                "stop_loss": trade.stop_loss,
                "take_profit": trade.take_profit,
                "mt5_ticket": trade.mt5_ticket,
                "reason": trade.reason,
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
                "stop_loss": trade.stop_loss,
                "take_profit": trade.take_profit,
                "mt5_ticket": trade.mt5_ticket,
                "reason": trade.reason,
                "created_at": trade.created_at.isoformat() if trade.created_at else None,
                "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
            })
        return JSONResponse({"success": True, "agent_user_id": MCP_AGENT_USER_ID, "trades": trade_list})
    except Exception as e:
        log_error(e, "api_agent_trades")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@mcp.custom_route("/api/agent/activity", methods=["POST"])
async def api_post_agent_activity(request: StarletteRequest) -> JSONResponse:
    """Hermes posts one per-scan observability record so silent scans are visible.
    Body: {event_type, symbol?, score?, decision?, detail?, trade_id?, agent?, ts?}
    — event_type defaults to 'scan'. Best-effort persistence (dropped on a
    Postgres outage). Auth mirrors the other write endpoints."""
    user_id, error_response = require_auth(request)
    if error_response:
        return error_response
    try:
        body = await request.json()

        ts = None
        ts_raw = body.get("ts")
        if ts_raw:
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                ts = None  # fall back to server time

        score = body.get("score")
        try:
            score = float(score) if score is not None else None
        except (ValueError, TypeError):
            score = None

        def _s(key):
            v = body.get(key)
            return str(v) if v is not None and str(v) != "" else None

        row = TradingRepository.record_agent_activity(
            event_type=str(body.get("event_type") or "scan"),
            agent=str(body.get("agent") or MCP_AGENT_USER_ID),
            symbol=_s("symbol"), score=score,
            decision=_s("decision"), detail=_s("detail"),
            trade_id=_s("trade_id"), ts=ts,
        )
        log_api_call("/api/agent/activity", "POST", 200)
        return JSONResponse({"success": True, "id": row.id if row else None})
    except Exception as e:
        log_error(e, "api_post_agent_activity")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@mcp.custom_route("/api/agent/activity", methods=["GET"])
def api_get_agent_activity(request: StarletteRequest) -> JSONResponse:
    """Recent per-scan agent activity for the TUI stream — no auth (read-only,
    mirrors /api/trades/agent). Query: ?limit=N (default 100, max 500)."""
    try:
        try:
            limit = max(1, min(int(request.query_params.get("limit", 100)), 500))
        except (ValueError, TypeError):
            limit = 100
        rows = TradingRepository.get_recent_agent_activity(limit=limit)
        activity = [{
            "id": r.id,
            "ts": r.ts.isoformat() if r.ts else None,
            "agent": r.agent,
            "event_type": r.event_type,
            "symbol": r.symbol,
            "score": r.score,
            "decision": r.decision,
            "detail": r.detail,
            "trade_id": r.trade_id,
        } for r in rows]
        return JSONResponse({"success": True, "activity": activity})
    except Exception as e:
        log_error(e, "api_get_agent_activity")
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

                if trade.mt5_ticket:
                    # MT5: the VPS monitor refreshes live price + floating P&L into
                    # the row each cycle; the broker-less cluster just serves them
                    # (no get_contract_status — that's Deriv-only).
                    current_price = float(trade.current_price or 0)
                    unrealized_pnl = float(trade.unrealized_pnl or 0)
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
                        "mt5_ticket": trade.mt5_ticket,
                        "reason": trade.reason,
                    })
                    continue

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
                    "mt5_ticket": trade.mt5_ticket,
                    "reason": trade.reason,
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


async def _balance_snapshot_loop() -> None:
    """Periodically refresh the Postgres portfolio balance snapshot via the active
    broker client. Runs only on the trader bot (DERIV_API_TOKEN edge or BROKER=mt5
    VPS) so the token-less in-cluster read-only backend can serve a current balance."""
    interval = int(os.getenv("TRADE_MONITOR_INTERVAL", "30"))
    while True:
        await asyncio.sleep(interval)
        try:
            # Replay any writes buffered locally while Postgres was unreachable.
            replayed = TradingRepository.flush_outbox()
            if replayed:
                logger.info("Replayed %d buffered write(s) to Postgres", replayed)
        except Exception as e:
            logger.warning("Outbox flush error: %s", e)
        try:
            await _do_get_balance()
        except Exception as e:
            logger.warning("Balance snapshot loop error: %s", e)


from notify import notify_telegram as _notify_telegram  # shared stdlib Telegram notifier


def _autotrade_side(score: int, threshold: int, longs_only: bool) -> Optional[str]:
    """Decide the trade side from a composite signal score.

    Returns 'buy', 'sell', or None (no trade). Refined ruleset 2026-06-16:
    longs-only by default — the demo edge lived entirely in the long
    mean-reversion signal (score +2: 75% win), while the mirror short lost
    money. Set AUTOTRADE_LONGS_ONLY=false to allow shorts again.
    """
    if longs_only:
        return "buy" if score >= threshold else None
    if abs(score) >= threshold:
        return "buy" if score > 0 else "sell"
    return None


def _atr_from_ohlc(highs: list, lows: list, closes: list, period: int = 14) -> Optional[float]:
    """Wilder's ATR from OHLC candles. Returns the latest ATR value, or None if
    there isn't enough data. True range = max(high-low, |high-prevclose|,
    |low-prevclose|). Used for volatility-adaptive stop sizing (research-backed)."""
    n = len(closes)
    if n < period + 1 or len(highs) != n or len(lows) != n:
        return None
    trs = []
    for i in range(1, n):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period          # seed
    k = 1.0 / period                          # Wilder smoothing
    for tr in trs[period:]:
        atr = atr * (1 - k) + tr * k
    return atr


def _size_position(balance: float, risk_pct: float, stop_dist: float,
                   tick_value: float, tick_size: float,
                   vmin: float, vmax: float, vstep: float) -> Optional[float]:
    """Fixed-fractional position size: pick the lot whose loss at the stop equals
    `risk_pct` of `balance`. Loss-per-lot = (stop_dist / tick_size) * tick_value.
    Clamps to [vmin, vmax] and rounds DOWN to vstep. Returns None if the inputs
    can't produce a valid lot (caller falls back to min lot)."""
    if min(balance, risk_pct, stop_dist, tick_value, tick_size, vmin) <= 0:
        return None
    loss_per_lot = (stop_dist / tick_size) * tick_value
    if loss_per_lot <= 0:
        return None
    raw = (balance * risk_pct) / loss_per_lot
    if vstep and vstep > 0:
        raw = (int(raw / vstep)) * vstep      # round down to a whole step
    lot = max(vmin, raw)
    if vmax and vmax > 0:
        lot = min(lot, vmax)
    # round to the step's decimal precision to avoid float dust (e.g. 0.30000000004)
    decimals = max(0, len(str(vstep).split(".")[-1])) if (vstep and "." in str(vstep)) else 2
    return round(lot, decimals)


def _loss_at_stop(lot: float, stop_dist: float,
                  tick_value: float, tick_size: float) -> float:
    """Dollar loss if `lot` is stopped out `stop_dist` away. Mirrors the
    loss-per-lot formula in `_size_position`. Returns 0.0 on invalid inputs
    (caller treats 0 as "can't price the risk" → does not gate on it)."""
    if min(lot, stop_dist, tick_value, tick_size) <= 0:
        return 0.0
    return (stop_dist / tick_size) * tick_value * lot


async def _autotrade_loop() -> None:
    """Deterministic (no-LLM) autotrader. Scans configured symbols on an interval,
    places a min-lot MT5 trade when |composite_score| >= threshold (reusing the
    same scoring as get_market_signal), and pings Telegram ONLY on a real entry.
    Zero LLM tokens. Gated by AUTOTRADE_ENABLED; MT5 only."""
    if BROKER != "mt5" or os.getenv("AUTOTRADE_ENABLED", "false").lower() != "true":
        return
    interval = int(os.getenv("AUTOTRADE_INTERVAL", "60"))
    threshold = int(os.getenv("AUTOTRADE_THRESHOLD", "2"))
    max_lot = float(os.getenv("AUTOTRADE_MAX_LOT", "0"))      # 0 ⇒ no manual cap (rely on 2% risk + broker vmax)
    rr = float(os.getenv("AUTOTRADE_RR", "2.0"))               # target = rr × stop (min 2:1, research-backed)
    sl_floor_pct = float(os.getenv("AUTOTRADE_SL_FLOOR_PCT", "0.002"))  # min stop as % of price
    atr_period = int(os.getenv("AUTOTRADE_ATR_PERIOD", "14"))
    atr_mult = float(os.getenv("AUTOTRADE_ATR_MULT", "1.5"))   # stop = atr_mult × ATR
    ema_period = int(os.getenv("AUTOTRADE_EMA_PERIOD", "50"))  # trend filter; 0 disables
    risk_pct = float(os.getenv("AUTOTRADE_RISK_PCT", "0.02"))  # fixed-fractional risk/trade; 0 ⇒ min lot
    # Affordability ceiling: skip a symbol when even its smallest tradeable lot
    # would risk more than this fraction of balance. Makes the symbol universe
    # self-select by account size (a $50 account can't afford a min-lot 1HZ25V).
    max_risk_pct = float(os.getenv("AUTOTRADE_MAX_RISK_PCT", "0.05"))
    timeframe = int(os.getenv("AUTOTRADE_TIMEFRAME", "900"))   # signal candle granularity (900=M15)
    longs_only = os.getenv("AUTOTRADE_LONGS_ONLY", "true").lower() == "true"
    symbols = [s.strip() for s in os.getenv(
        "AUTOTRADE_SYMBOLS", "R_75").split(",") if s.strip()]
    user_id = get_user_id()
    need_bars = max(35, ema_period + 1, atr_period + 1)
    logger.info("Autotrader ON: every %ds, tf=%ds, score>=+%d, longs_only=%s, EMA%d trend, "
                "stop=%.1f×ATR%d (floor %.2f%%), RR=%.1f, risk=%.1f%%/trade, maxDD=%.0f%%, symbols %s",
                interval, timeframe, threshold, longs_only, ema_period, atr_mult, atr_period,
                sl_floor_pct * 100, rr, risk_pct * 100, MAX_DRAWDOWN_PCT * 100, symbols)
    # Startup heartbeat so we always know the autotrader came up.
    await asyncio.to_thread(_notify_telegram,
        f"🤖 Autotrader restarted | symbols: {', '.join(symbols)} | "
        f"score >= +{threshold} | risk={risk_pct*100:.0f}%/trade | "
        f"stop={atr_mult}xATR{atr_period} | maxDD={MAX_DRAWDOWN_PCT*100:.0f}%")
    await asyncio.sleep(10)  # let the bridge/monitor settle after startup
    dd_halted = False
    while True:
        try:
            # Max-drawdown kill-switch: stop opening new trades once equity has
            # fallen MAX_DRAWDOWN_PCT below its peak. Telegram-notify once on trip.
            dd_ok, dd_msg = check_max_drawdown(user_id)
            if not dd_ok:
                if not dd_halted:
                    logger.warning("Autotrader halted — %s", dd_msg)
                    await asyncio.to_thread(_notify_telegram,
                        f"🛑 Autotrader halted: {dd_msg}. No new trades until reviewed.")
                    dd_halted = True
                await asyncio.sleep(interval)
                continue
            dd_halted = False

            client = await get_deriv_client()
            try:
                open_syms = {p.get("symbol") for p in (await client.get_open_positions() or [])}
            except Exception:
                open_syms = set()

            # Account balance for fixed-fractional sizing (once per cycle).
            balance = 0.0
            if risk_pct > 0:
                try:
                    bal = await client.get_account_balance()
                    bd = bal.get("balance", {}) if "error" not in bal else {}
                    balance = float(bd.get("balance") or bd.get("equity") or 0)
                except Exception:
                    balance = 0.0

            for code in symbols:
                name = get_symbol_display_name(code)
                if name in open_syms:
                    continue  # one position per symbol — don't stack

                # Signal + ATR from candles on the configured timeframe (M15 by
                # default — M1 is too noisy for reliable signals per the research).
                resp = await client.get_candles(name, granularity=timeframe, count=200)
                candles = resp.get("candles", []) if "error" not in resp else []
                if len(candles) < need_bars:
                    continue
                closes = [c["close"] for c in candles]
                highs = [c["high"] for c in candles]
                lows = [c["low"] for c in candles]
                price = closes[-1]

                sig = _compute_market_signal(name, closes)
                score = sig.get("composite_score", 0)
                side = _autotrade_side(score, threshold, longs_only)
                if side is None:
                    continue

                # Trend filter: only trade with the EMA trend (buy above, sell
                # below). Formalises the long/uptrend edge the demo data showed.
                if ema_period > 0:
                    ema_vals = TechnicalIndicators.calculate_ema(closes, ema_period)
                    ema = next((x for x in reversed(ema_vals) if x is not None), None)
                    if ema is not None:
                        if side == "buy" and price <= ema:
                            continue
                        if side == "sell" and price >= ema:
                            continue

                specs = await client.get_symbol_specs(name)
                vmin = specs.get("volume_min") or 0
                vmax = specs.get("volume_max") or 0
                vstep = specs.get("volume_step") or 0
                if not vmin:
                    continue

                # Stop = atr_mult × ATR, floored by % of price and MT5's min stop;
                # target = RR × stop. SL/TP attached from the real fill downstream.
                atr = _atr_from_ohlc(highs, lows, closes, atr_period)
                if not atr or atr <= 0:
                    continue
                min_stop = specs.get("stops_level", 0) * specs.get("point", 0.0)
                sl_dist = max(atr_mult * atr, sl_floor_pct * price, min_stop * 1.5)
                tp_dist = rr * sl_dist

                # Position size: fixed-fractional (risk_pct of balance); fall back
                # to min lot when balance/tick data is unavailable.
                lot = vmin
                if risk_pct > 0 and balance > 0:
                    sized = _size_position(
                        balance, risk_pct, sl_dist,
                        specs.get("tick_value", 0.0), specs.get("tick_size", 0.0),
                        vmin, vmax, vstep)
                    if sized:
                        lot = sized

                # Affordability gate: _size_position floors UP to the broker min
                # lot, so on a small account a min-lot trade on a high-notional
                # symbol can risk far more than the account holds. Skip the symbol
                # when the chosen lot's risk-at-stop exceeds max_risk_pct of balance.
                if balance > 0 and max_risk_pct > 0:
                    risk_at_stop = _loss_at_stop(
                        lot, sl_dist,
                        specs.get("tick_value", 0.0), specs.get("tick_size", 0.0))
                    if risk_at_stop > max_risk_pct * balance:
                        logger.info(
                            "Autotrade skip %s: min-lot risk $%.2f > %.0f%% of $%.2f balance",
                            name, risk_at_stop, max_risk_pct * 100, balance)
                        continue

                if max_lot > 0 and lot > max_lot:
                    logger.info("Autotrade skip %s: sized lot %.4f > max_lot %.4f", name, lot, max_lot)
                    continue

                reason = (f"auto score {score:+d}: {sig.get('reason', '')} "
                          f"[EMA{ema_period} trend, {atr_mult}×ATR stop, {risk_pct*100:.0f}% risk]")
                result = await _do_place_trade_mt5(
                    name, side, lot, sl_dist=sl_dist, tp_dist=tp_dist, reason=reason)
                if "placed" in result.lower():
                    logger.info("Autotrade: %s", result)
                    await asyncio.to_thread(_notify_telegram, f"🤖 {result}\n↳ {reason}")
                else:
                    logger.info("Autotrade skip %s: %s", name, result)
        except Exception as e:
            logger.warning("Autotrade loop error: %s", e)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    # The trade monitor + balance-snapshot/outbox-flush loop are started by
    # _app_lifespan on the server's event loop (see above). uvicorn owns SIGTERM/
    # SIGINT and drives the ASGI lifespan shutdown, so no manual loop or signal
    # handlers are needed here (the old ones ran on an abandoned loop and never
    # fired the background tasks).
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

