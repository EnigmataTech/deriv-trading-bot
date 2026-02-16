"""
Structured JSON logging module for the trading bot.

Provides centralized logging configuration with JSON-formatted output,
supporting both console and file handlers with contextual information.

Usage:
    from logger import setup_logging, get_logger, log_trade_placed

    setup_logging(level="DEBUG", log_file="trading.log")
    logger = get_logger("main")
    logger.info("Server started", extra={"port": 8000})
    log_trade_placed(symbol="R_50", amount=10, direction="CALL")
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Optional


# Patterns for sensitive data that should be redacted
_token_patterns = [
    (re.compile(r'(api[_-]?token|authorization|bearer|password|secret|api[_-]?key)\s*[:=]\s*["\']?([^"\'\s,}]+)', re.IGNORECASE), r'\1: [REDACTED]'),
    (re.compile(r'(Bearer\s+)([A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+)', re.IGNORECASE), r'\1[REDACTED_JWT]'),
    (re.compile(r'"authorize"\s*:\s*"([^"]+)"'), r'"authorize": "[REDACTED]"'),
]

# Add custom token pattern from env var if set
_custom_token = os.getenv("REDACT_TOKEN_PATTERN")
if _custom_token:
    _token_patterns.append((re.compile(re.escape(_custom_token)), '[REDACTED_TOKEN]'))

SENSITIVE_PATTERNS = _token_patterns


def sanitize_sensitive_data(data: Any) -> Any:
    """Recursively sanitize sensitive data from log entries.

    Args:
        data: Data to sanitize (can be string, dict, list, or other)

    Returns:
        Sanitized data with sensitive information redacted
    """
    if isinstance(data, str):
        result = data
        for pattern, replacement in SENSITIVE_PATTERNS:
            result = pattern.sub(replacement, result)
        return result
    elif isinstance(data, dict):
        sanitized = {}
        sensitive_keys = {'password', 'secret', 'token', 'api_token', 'api_key',
                         'authorization', 'bearer', 'credential', 'private_key'}
        for key, value in data.items():
            key_lower = key.lower()
            if any(sensitive in key_lower for sensitive in sensitive_keys):
                sanitized[key] = '[REDACTED]'
            else:
                sanitized[key] = sanitize_sensitive_data(value)
        return sanitized
    elif isinstance(data, list):
        return [sanitize_sensitive_data(item) for item in data]
    else:
        return data


class JSONFormatter(logging.Formatter):
    """Custom formatter that outputs log records as JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as a JSON string.

        Args:
            record: The log record to format.

        Returns:
            JSON-formatted string containing log data.
        """
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add location info for debugging
        if record.levelno >= logging.DEBUG:
            log_entry["location"] = {
                "file": record.filename,
                "line": record.lineno,
                "function": record.funcName,
            }

        # Add any extra fields passed via the extra parameter
        # Standard LogRecord attributes to exclude from extras
        standard_attrs = {
            "name", "msg", "args", "created", "filename", "funcName",
            "levelname", "levelno", "lineno", "module", "msecs",
            "pathname", "process", "processName", "relativeCreated",
            "stack_info", "exc_info", "exc_text", "thread", "threadName",
            "taskName", "message",
        }

        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in standard_attrs and not key.startswith("_")
        }

        if extras:
            log_entry["context"] = extras

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Sanitize sensitive data before output
        sanitized_entry = sanitize_sensitive_data(log_entry)

        return json.dumps(sanitized_entry, default=str)


# Module-level state
_root_logger: Optional[logging.Logger] = None
_log_file: Optional[str] = None


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """Initialize the logging system with JSON formatting.

    Configures the root logger with JSON-formatted output to console
    and optionally to a file. Clears any existing handlers first.

    Args:
        level: Logging level as string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_file: Optional path to log file. If provided, logs are written
                  to both console and file.

    Returns:
        The configured root logger instance.

    Example:
        setup_logging(level="DEBUG", log_file="trading.log")
    """
    global _root_logger, _log_file

    # Convert string level to logging constant
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Get or create the root logger for our application
    _root_logger = logging.getLogger("trading_bot")
    _root_logger.setLevel(numeric_level)

    # Clear any existing handlers
    _root_logger.handlers.clear()

    # Create JSON formatter
    formatter = JSONFormatter()

    # Console handler - always added
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    _root_logger.addHandler(console_handler)

    # File handler - added if log_file is specified
    if log_file:
        _log_file = log_file
        file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        _root_logger.addHandler(file_handler)

    # Prevent propagation to avoid duplicate logs
    _root_logger.propagate = False

    return _root_logger


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the specified name.

    Returns a child logger under the trading_bot namespace.
    If setup_logging() hasn't been called, initializes with defaults.

    Args:
        name: Name for the logger (will be prefixed with 'trading_bot.').

    Returns:
        Configured logger instance.

    Example:
        logger = get_logger("api")
        logger.info("Request received", extra={"endpoint": "/trades"})
    """
    global _root_logger

    if _root_logger is None:
        setup_logging()

    return logging.getLogger(f"trading_bot.{name}")


def log_trade_placed(
    symbol: str,
    amount: float,
    direction: str,
    contract_type: Optional[str] = None,
    duration: Optional[int] = None,
    trade_id: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """Log a trade placement event with structured context.

    Args:
        symbol: Trading symbol (e.g., "R_50", "EURUSD").
        amount: Trade amount/stake.
        direction: Trade direction ("CALL", "PUT", "BUY", "SELL").
        contract_type: Optional contract type (e.g., "DIGITDIFF").
        duration: Optional duration in ticks/seconds.
        trade_id: Optional trade identifier.
        **kwargs: Additional context fields to include.

    Example:
        log_trade_placed(
            symbol="R_50",
            amount=10,
            direction="CALL",
            duration=5,
            strategy="martingale"
        )
    """
    logger = get_logger("trades")

    context = {
        "event": "trade_placed",
        "symbol": symbol,
        "amount": amount,
        "direction": direction.upper(),
    }

    if contract_type:
        context["contract_type"] = contract_type
    if duration:
        context["duration"] = duration
    if trade_id:
        context["trade_id"] = trade_id

    context.update(kwargs)

    logger.info(
        f"Trade placed: {direction.upper()} {symbol} @ {amount}",
        extra=context,
    )


def log_trade_closed(
    symbol: str,
    amount: float,
    profit: float,
    result: str,
    trade_id: Optional[str] = None,
    entry_price: Optional[float] = None,
    exit_price: Optional[float] = None,
    **kwargs: Any,
) -> None:
    """Log a trade closure event with profit/loss information.

    Args:
        symbol: Trading symbol.
        amount: Original trade amount.
        profit: Profit or loss amount (negative for loss).
        result: Trade result ("WIN", "LOSS", "TIE").
        trade_id: Optional trade identifier.
        entry_price: Optional entry price.
        exit_price: Optional exit price.
        **kwargs: Additional context fields.

    Example:
        log_trade_closed(
            symbol="R_50",
            amount=10,
            profit=8.5,
            result="WIN",
            trade_id="T123"
        )
    """
    logger = get_logger("trades")

    context = {
        "event": "trade_closed",
        "symbol": symbol,
        "amount": amount,
        "profit": profit,
        "result": result.upper(),
    }

    if trade_id:
        context["trade_id"] = trade_id
    if entry_price is not None:
        context["entry_price"] = entry_price
    if exit_price is not None:
        context["exit_price"] = exit_price

    context.update(kwargs)

    log_level = logging.INFO if profit >= 0 else logging.WARNING
    logger.log(
        log_level,
        f"Trade closed: {result.upper()} on {symbol}, profit: {profit:+.2f}",
        extra=context,
    )


def log_api_call(
    endpoint: str,
    method: str = "GET",
    status_code: Optional[int] = None,
    duration_ms: Optional[float] = None,
    request_id: Optional[str] = None,
    success: bool = True,
    **kwargs: Any,
) -> None:
    """Log an API call with request/response details.

    Args:
        endpoint: API endpoint or URL called.
        method: HTTP method (GET, POST, etc.).
        status_code: Optional HTTP status code.
        duration_ms: Optional request duration in milliseconds.
        request_id: Optional request identifier for tracing.
        success: Whether the call was successful.
        **kwargs: Additional context fields.

    Example:
        log_api_call(
            endpoint="/api/v1/authorize",
            method="POST",
            status_code=200,
            duration_ms=145.3,
            success=True
        )
    """
    logger = get_logger("api")

    context = {
        "event": "api_call",
        "endpoint": endpoint,
        "method": method.upper(),
        "success": success,
    }

    if status_code is not None:
        context["status_code"] = status_code
    if duration_ms is not None:
        context["duration_ms"] = round(duration_ms, 2)
    if request_id:
        context["request_id"] = request_id

    context.update(kwargs)

    log_level = logging.INFO if success else logging.WARNING
    status_str = f" [{status_code}]" if status_code else ""
    duration_str = f" ({duration_ms:.1f}ms)" if duration_ms else ""

    logger.log(
        log_level,
        f"API {method.upper()} {endpoint}{status_str}{duration_str}",
        extra=context,
    )


def log_error(
    message: str,
    error: Optional[Exception] = None,
    error_code: Optional[str] = None,
    component: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """Log an error event with exception details.

    Args:
        message: Human-readable error message.
        error: Optional exception object.
        error_code: Optional error code for categorization.
        component: Optional component where error occurred.
        **kwargs: Additional context fields.

    Example:
        try:
            risky_operation()
        except Exception as e:
            log_error(
                "Failed to execute trade",
                error=e,
                error_code="TRADE_EXEC_FAILED",
                component="trade_executor"
            )
    """
    logger = get_logger("errors")

    context = {
        "event": "error",
    }

    if error:
        context["error_type"] = type(error).__name__
        context["error_message"] = str(error)
    if error_code:
        context["error_code"] = error_code
    if component:
        context["component"] = component

    context.update(kwargs)

    logger.error(
        message,
        extra=context,
        exc_info=error is not None,
    )


def log_websocket_event(
    event_type: str,
    connection_id: Optional[str] = None,
    message_type: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """Log WebSocket connection events.

    Args:
        event_type: Type of event (connected, disconnected, message, error).
        connection_id: Optional connection identifier.
        message_type: Optional message type for message events.
        **kwargs: Additional context fields.

    Example:
        log_websocket_event(
            event_type="connected",
            connection_id="ws_123",
            server="wss://ws.derivws.com/websockets/v3"
        )
    """
    logger = get_logger("websocket")

    context = {
        "event": f"websocket_{event_type}",
    }

    if connection_id:
        context["connection_id"] = connection_id
    if message_type:
        context["message_type"] = message_type

    context.update(kwargs)

    log_level = logging.ERROR if event_type == "error" else logging.INFO
    logger.log(
        log_level,
        f"WebSocket {event_type}",
        extra=context,
    )


def log_balance_update(
    balance: float,
    currency: str = "USD",
    change: Optional[float] = None,
    account_id: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """Log account balance updates.

    Args:
        balance: Current balance.
        currency: Currency code (default: USD).
        change: Optional balance change amount.
        account_id: Optional account identifier.
        **kwargs: Additional context fields.

    Example:
        log_balance_update(
            balance=1500.50,
            currency="USD",
            change=-10.00,
            account_id="CR123456"
        )
    """
    logger = get_logger("account")

    context = {
        "event": "balance_update",
        "balance": balance,
        "currency": currency,
    }

    if change is not None:
        context["change"] = change
    if account_id:
        context["account_id"] = account_id

    context.update(kwargs)

    change_str = f" ({change:+.2f})" if change is not None else ""
    logger.info(
        f"Balance: {balance:.2f} {currency}{change_str}",
        extra=context,
    )


def log_audit_auth(
    event_type: str,
    user_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    success: bool = True,
    reason: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """Log authentication audit events.

    Args:
        event_type: Type of auth event (login, logout, token_refresh, failed_login).
        user_id: User identifier (if known).
        ip_address: Client IP address.
        success: Whether the auth attempt was successful.
        reason: Optional reason for failure.
        **kwargs: Additional context fields.

    Example:
        log_audit_auth(
            event_type="login",
            user_id="user_123",
            ip_address="192.168.1.1",
            success=True
        )
    """
    logger = get_logger("audit.auth")

    context = {
        "event": f"auth_{event_type}",
        "audit_type": "authentication",
        "success": success,
    }

    if user_id:
        context["user_id"] = user_id
    if ip_address:
        context["ip_address"] = ip_address
    if reason:
        context["reason"] = reason

    context.update(kwargs)

    log_level = logging.INFO if success else logging.WARNING
    status_str = "SUCCESS" if success else "FAILED"
    logger.log(
        log_level,
        f"AUDIT: Auth {event_type} {status_str} for user={user_id or 'unknown'}",
        extra=context,
    )


def log_audit_trade(
    event_type: str,
    user_id: str,
    trade_id: Optional[str] = None,
    symbol: Optional[str] = None,
    amount: Optional[float] = None,
    direction: Optional[str] = None,
    success: bool = True,
    reason: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """Log trade audit events.

    Args:
        event_type: Type of trade event (place, close, sell, blocked).
        user_id: User identifier.
        trade_id: Trade/contract identifier.
        symbol: Trading symbol.
        amount: Trade amount.
        direction: Trade direction (CALL/PUT).
        success: Whether the trade action was successful.
        reason: Optional reason for failure or blocking.
        **kwargs: Additional context fields.

    Example:
        log_audit_trade(
            event_type="place",
            user_id="user_123",
            trade_id="T456",
            symbol="R_50",
            amount=10,
            direction="CALL",
            success=True
        )
    """
    logger = get_logger("audit.trade")

    context = {
        "event": f"trade_{event_type}",
        "audit_type": "trade",
        "user_id": user_id,
        "success": success,
    }

    if trade_id:
        context["trade_id"] = trade_id
    if symbol:
        context["symbol"] = symbol
    if amount is not None:
        context["amount"] = amount
    if direction:
        context["direction"] = direction
    if reason:
        context["reason"] = reason

    context.update(kwargs)

    log_level = logging.INFO if success else logging.WARNING
    status_str = "SUCCESS" if success else "FAILED"
    trade_details = f"symbol={symbol or 'N/A'}, amount={amount or 'N/A'}"
    logger.log(
        log_level,
        f"AUDIT: Trade {event_type} {status_str} for user={user_id} ({trade_details})",
        extra=context,
    )


if __name__ == "__main__":
    # Demo usage
    setup_logging(level="DEBUG", log_file="demo_trading.log")

    logger = get_logger("main")
    logger.info("Trading bot initialized", extra={"version": "1.0.0", "port": 8000})

    log_trade_placed(symbol="R_50", amount=10, direction="CALL", duration=5)
    log_trade_closed(symbol="R_50", amount=10, profit=8.5, result="WIN")
    log_api_call(endpoint="/api/v1/authorize", method="POST", status_code=200, duration_ms=145.3)
    log_balance_update(balance=1008.50, currency="USD", change=8.50)

    try:
        raise ValueError("Demo error for testing")
    except Exception as e:
        log_error("Something went wrong", error=e, error_code="DEMO_ERROR", component="main")

    log_websocket_event(event_type="connected", connection_id="ws_001")

    print("\nDemo complete! Check demo_trading.log for file output.")
