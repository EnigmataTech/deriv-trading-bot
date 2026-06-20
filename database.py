import logging
import os
from dotenv import load_dotenv
load_dotenv()
from sqlalchemy import create_engine, Column, Integer, String, Text, Float, DateTime, Boolean
from sqlalchemy.exc import OperationalError, InterfaceError, DBAPIError
from sqlalchemy.orm import declarative_base, sessionmaker
from typing import List, Optional
from datetime import datetime

import outbox

logger = logging.getLogger(__name__)


def _is_conn_error(exc: Exception) -> bool:
    """True when a write failed because Postgres was unreachable (so it's worth
    buffering locally), as opposed to a data/logic error (which should surface)."""
    if isinstance(exc, (OperationalError, InterfaceError)):
        return True
    if isinstance(exc, DBAPIError):
        return bool(getattr(exc, "connection_invalidated", False))
    return False


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:***@localhost:5432/trading")
# connect_timeout/statement_timeout are psycopg-only options; SQLite (used by the
# test suite) rejects them, so only pass them for a Postgres URL.
_connect_args = (
    {
        "connect_timeout": 5,            # fail fast when Postgres is unreachable
        "options": "-c statement_timeout=10000",  # 10s query timeout, prevent slow-query hangs
    }
    if DATABASE_URL.startswith(("postgresql", "postgres"))
    else {}
)
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args=_connect_args,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def migrate_database():
    """Add new columns to existing database if they don't exist."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = inspector.get_table_names()

    # create_all adds NEW tables but never ALTERs existing ones, so back-fill
    # columns added after a table first shipped. {table: {column: SQL type}}.
    migrations = {
        'trades': {
            'stop_loss': 'FLOAT',
            'take_profit': 'FLOAT',
            'trailing_stop_distance': 'FLOAT',
            'trailing_stop_price': 'FLOAT',
            'highest_price_seen': 'FLOAT',
            'mt5_ticket': 'BIGINT',
            'reason': 'TEXT',
            'current_price': 'FLOAT',
            'unrealized_pnl': 'FLOAT',
        },
        'portfolios': {
            'peak_equity': 'FLOAT NOT NULL DEFAULT 0.0',  # max-drawdown high-water mark
        },
    }

    with engine.connect() as conn:
        for table, cols in migrations.items():
            if table not in tables:
                continue  # will be created by create_all
            existing = [c['name'] for c in inspector.get_columns(table)]
            for col_name, col_type in cols.items():
                if col_name not in existing:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {col_name} {col_type}'))
                    conn.commit()


# Run migration before create_all. Tolerate Postgres being unreachable at
# startup (e.g. tunnel down) so the bot still boots and buffers writes locally;
# the tables already exist on the long-running cluster Postgres.
try:
    migrate_database()
except Exception as e:
    logger.warning("Skipping schema migration — Postgres unreachable at startup: %s", e)

class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, nullable=False, index=True)
    trade_id = Column(String, nullable=False, unique=True)
    symbol = Column(String, nullable=False)
    trade_type = Column(String, nullable=False)  # 'buy' or 'sell'
    amount = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    profit_loss = Column(Float, nullable=True)
    status = Column(String, nullable=False, default='open')  # 'open', 'closed'
    created_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)
    # Stop-loss and take-profit fields
    stop_loss = Column(Float, nullable=True)  # Fixed stop-loss price
    take_profit = Column(Float, nullable=True)  # Fixed take-profit price
    trailing_stop_distance = Column(Float, nullable=True)  # Trailing stop distance in points
    trailing_stop_price = Column(Float, nullable=True)  # Current trailing stop price (moves up)
    highest_price_seen = Column(Float, nullable=True)  # Highest price since trade opened (for trailing)
    mt5_ticket = Column(Integer, nullable=True, index=True)  # MT5 position/order ticket (BROKER=mt5)
    reason = Column(Text, nullable=True)  # Agent's stated rationale for the trade (Hermes)
    current_price = Column(Float, nullable=True)  # Live price, refreshed by the MT5 monitor
    unrealized_pnl = Column(Float, nullable=True)  # Live floating P&L, refreshed by the MT5 monitor

class Portfolio(Base):
    __tablename__ = "portfolios"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, nullable=False, index=True)
    balance = Column(Float, nullable=False, default=0.0)
    equity = Column(Float, nullable=False, default=0.0)
    margin = Column(Float, nullable=False, default=0.0)
    free_margin = Column(Float, nullable=False, default=0.0)
    peak_equity = Column(Float, nullable=False, default=0.0)  # high-water mark for max-drawdown kill-switch
    updated_at = Column(DateTime, default=datetime.utcnow)


class StrategyReport(Base):
    """Periodic strategy-performance snapshot, produced by the weekly report job.
    Kept as a durable, queryable record of how the refined ruleset is doing over
    time (see scripts/weekly_report.py)."""
    __tablename__ = "strategy_reports"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    period_days = Column(Integer, nullable=False)   # lookback window for this report
    total_trades = Column(Integer, nullable=False, default=0)
    win_rate = Column(Float, nullable=False, default=0.0)        # 0..1
    total_pnl = Column(Float, nullable=False, default=0.0)
    profit_factor = Column(Float, nullable=False, default=0.0)
    report_text = Column(Text, nullable=False)       # full human-readable report


try:
    Base.metadata.create_all(bind=engine)
except Exception as e:
    logger.warning("Skipping create_all — Postgres unreachable at startup: %s", e)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class TradingRepository:

    @staticmethod
    def get_trades_by_user(user_id: str) -> List[Trade]:
        db = SessionLocal()
        try:
            return db.query(Trade).filter(Trade.user_id == user_id).all()
        finally:
            db.close()

    @staticmethod
    def create_trade(user_id: str, trade_id: str, symbol: str, trade_type: str, amount: float, entry_price: float) -> Trade:
        db = SessionLocal()
        try:
            # Check if trade already exists
            existing_trade = db.query(Trade).filter_by(trade_id=trade_id).first()
            if existing_trade:
                return existing_trade

            trade = Trade(
                user_id=user_id,
                trade_id=trade_id,
                symbol=symbol,
                trade_type=trade_type,
                amount=amount,
                entry_price=entry_price
            )
            db.add(trade)
            db.commit()
            db.refresh(trade)
            return trade
        finally:
            db.close()

    @staticmethod
    def update_portfolio(user_id: str, balance: float, equity: float, margin: float, free_margin: float) -> Optional[Portfolio]:
        """Upsert the balance snapshot. Buffers to the outbox if Postgres is
        unreachable (returns a transient Portfolio in that case)."""
        try:
            return TradingRepository._pg_update_portfolio(user_id, balance, equity, margin, free_margin)
        except Exception as e:
            if not _is_conn_error(e):
                raise
            outbox.enqueue("update_portfolio", {
                "user_id": user_id, "balance": balance, "equity": equity,
                "margin": margin, "free_margin": free_margin,
            })
            return Portfolio(
                user_id=user_id, balance=balance, equity=equity,
                margin=margin, free_margin=free_margin,
            )

    @staticmethod
    def _pg_update_portfolio(user_id: str, balance: float, equity: float, margin: float, free_margin: float) -> Portfolio:
        db = SessionLocal()
        try:
            portfolio = db.query(Portfolio).filter(Portfolio.user_id == user_id).first()
            if portfolio:
                portfolio.balance = balance
                portfolio.equity = equity
                portfolio.margin = margin
                portfolio.free_margin = free_margin
                # Track the equity high-water mark for the max-drawdown kill-switch.
                portfolio.peak_equity = max(portfolio.peak_equity or 0.0, equity)
                portfolio.updated_at = datetime.utcnow()
            else:
                portfolio = Portfolio(
                    user_id=user_id,
                    balance=balance,
                    equity=equity,
                    margin=margin,
                    free_margin=free_margin,
                    peak_equity=equity,
                )
                db.add(portfolio)
            db.commit()
            db.refresh(portfolio)
            return portfolio
        finally:
            db.close()

    @staticmethod
    def _apply_op(op: str, payload: dict) -> None:
        """Replay a single buffered op via the raw Postgres writers. Raises on a
        connection failure so the outbox keeps the op for a later attempt."""
        if op == "create_trade_with_sl_tp":
            p = dict(payload)
            ca = p.pop("created_at", None)
            p["created_at"] = datetime.fromisoformat(ca) if ca else None
            TradingRepository._pg_create_trade_with_sl_tp(**p)
        elif op == "update_trade_result":
            p = dict(payload)
            ca = p.pop("closed_at", None)
            p["closed_at"] = datetime.fromisoformat(ca) if ca else None
            TradingRepository._pg_update_trade_result(**p)
        elif op == "update_portfolio":
            TradingRepository._pg_update_portfolio(**payload)
        else:
            logger.error("Outbox: unknown op '%s' — dropping", op)

    @staticmethod
    def update_live_price(trade_id: str, current_price: float, unrealized_pnl: float) -> None:
        """Refresh an open trade's live price + floating P&L (best-effort: this is
        re-written every monitor cycle, so a transient Postgres outage is fine to
        skip — no outbox buffering)."""
        try:
            db = SessionLocal()
            try:
                trade = db.query(Trade).filter(Trade.trade_id == trade_id).first()
                if trade:
                    trade.current_price = current_price
                    trade.unrealized_pnl = unrealized_pnl
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            if not _is_conn_error(e):
                raise
            logger.debug("update_live_price skipped (Postgres unreachable): %s", e)

    @staticmethod
    def flush_outbox() -> int:
        """Replay any locally-buffered writes to Postgres. Safe to call often:
        a no-op when the outbox is empty, and stops early if Postgres is still
        down. Returns the number of ops replayed."""
        if outbox.pending_count() == 0:
            return 0
        return outbox.replay(TradingRepository._apply_op)

    @staticmethod
    def get_portfolio(user_id: str) -> Optional[Portfolio]:
        db = SessionLocal()
        try:
            return db.query(Portfolio).filter(Portfolio.user_id == user_id).first()
        finally:
            db.close()

    @staticmethod
    def get_open_trades(user_id: str = None) -> List[Trade]:
        """Get all trades with status='open'. If user_id is None, get all open trades across all users."""
        db = SessionLocal()
        try:
            query = db.query(Trade).filter(Trade.status == 'open')
            if user_id is not None:
                query = query.filter(Trade.user_id == user_id)
            return query.all()
        finally:
            db.close()

    @staticmethod
    def update_trade_result(trade_id: str, exit_price: float, profit_loss: float, status: str = 'closed') -> Optional[Trade]:
        """Update a trade with its final result. Buffers to the outbox if Postgres
        is unreachable (returns None in that case)."""
        try:
            return TradingRepository._pg_update_trade_result(trade_id, exit_price, profit_loss, status)
        except Exception as e:
            if not _is_conn_error(e):
                raise
            outbox.enqueue("update_trade_result", {
                "trade_id": trade_id, "exit_price": exit_price,
                "profit_loss": profit_loss, "status": status,
                "closed_at": datetime.utcnow().isoformat(),
            })
            return None

    @staticmethod
    def _pg_update_trade_result(
        trade_id: str, exit_price: float, profit_loss: float,
        status: str = 'closed', closed_at: Optional[datetime] = None,
    ) -> Optional[Trade]:
        """Update a trade with its final result (raw Postgres write)."""
        db = SessionLocal()
        try:
            trade = db.query(Trade).filter(Trade.trade_id == trade_id).first()
            if trade:
                trade.exit_price = exit_price
                trade.profit_loss = profit_loss
                trade.status = status
                trade.closed_at = closed_at or datetime.utcnow()
                db.commit()
                db.refresh(trade)
            return trade
        finally:
            db.close()

    @staticmethod
    def get_trade_by_trade_id(trade_id: str) -> Optional[Trade]:
        """Get a single trade by its trade_id (the Deriv contract ID)."""
        db = SessionLocal()
        try:
            return db.query(Trade).filter(Trade.trade_id == trade_id).first()
        finally:
            db.close()

    @staticmethod
    def get_trades_summary(user_id: str) -> dict:
        """Return a dict with summary stats: total_trades, open_trades, closed_trades, total_profit_loss, win_rate."""
        db = SessionLocal()
        try:
            trades = db.query(Trade).filter(Trade.user_id == user_id).all()

            total_trades = len(trades)
            open_trades = sum(1 for t in trades if t.status == 'open')
            closed_trades = sum(1 for t in trades if t.status == 'closed')

            closed_trade_list = [t for t in trades if t.status == 'closed']
            total_profit_loss = sum(t.profit_loss or 0 for t in closed_trade_list)

            winning_trades = sum(1 for t in closed_trade_list if (t.profit_loss or 0) > 0)
            win_rate = (winning_trades / closed_trades * 100) if closed_trades > 0 else 0.0

            return {
                'total_trades': total_trades,
                'open_trades': open_trades,
                'closed_trades': closed_trades,
                'total_profit_loss': total_profit_loss,
                'win_rate': win_rate
            }
        finally:
            db.close()

    @staticmethod
    def create_trade_with_sl_tp(
        user_id: str,
        trade_id: str,
        symbol: str,
        trade_type: str,
        amount: float,
        entry_price: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        trailing_stop_distance: Optional[float] = None,
        mt5_ticket: Optional[int] = None,
        reason: Optional[str] = None
    ) -> Optional[Trade]:
        """Create a trade with optional SL/TP settings.

        If Postgres is unreachable the write is buffered to the local outbox and
        a transient (unpersisted) Trade is returned so callers still see it.
        """
        try:
            return TradingRepository._pg_create_trade_with_sl_tp(
                user_id=user_id, trade_id=trade_id, symbol=symbol,
                trade_type=trade_type, amount=amount, entry_price=entry_price,
                stop_loss=stop_loss, take_profit=take_profit,
                trailing_stop_distance=trailing_stop_distance, mt5_ticket=mt5_ticket,
                reason=reason,
            )
        except Exception as e:
            if not _is_conn_error(e):
                raise
            outbox.enqueue("create_trade_with_sl_tp", {
                "user_id": user_id, "trade_id": trade_id, "symbol": symbol,
                "trade_type": trade_type, "amount": amount, "entry_price": entry_price,
                "stop_loss": stop_loss, "take_profit": take_profit,
                "trailing_stop_distance": trailing_stop_distance, "mt5_ticket": mt5_ticket,
                "reason": reason,
                "created_at": datetime.utcnow().isoformat(),
            })
            return Trade(
                user_id=user_id, trade_id=trade_id, symbol=symbol,
                trade_type=trade_type, amount=amount, entry_price=entry_price,
                stop_loss=stop_loss, take_profit=take_profit,
                trailing_stop_distance=trailing_stop_distance, mt5_ticket=mt5_ticket,
                reason=reason,
            )

    @staticmethod
    def _pg_create_trade_with_sl_tp(
        user_id: str,
        trade_id: str,
        symbol: str,
        trade_type: str,
        amount: float,
        entry_price: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        trailing_stop_distance: Optional[float] = None,
        mt5_ticket: Optional[int] = None,
        reason: Optional[str] = None,
        created_at: Optional[datetime] = None,
    ) -> Trade:
        """Create a trade with optional SL/TP settings (raw Postgres write)."""
        db = SessionLocal()
        try:
            # Check if trade already exists
            existing_trade = db.query(Trade).filter_by(trade_id=trade_id).first()
            if existing_trade:
                return existing_trade
            # Initialize trailing stop price if trailing stop is set
            trailing_stop_price = None
            highest_price_seen = None
            if trailing_stop_distance is not None and entry_price:
                highest_price_seen = entry_price
                if trade_type.lower() == 'call':
                    # For CALL, trailing stop is below entry
                    trailing_stop_price = entry_price - trailing_stop_distance
                else:
                    # For PUT, trailing stop is above entry
                    trailing_stop_price = entry_price + trailing_stop_distance

            trade = Trade(
                user_id=user_id,
                trade_id=trade_id,
                symbol=symbol,
                trade_type=trade_type,
                amount=amount,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_stop_distance=trailing_stop_distance,
                trailing_stop_price=trailing_stop_price,
                highest_price_seen=highest_price_seen,
                mt5_ticket=mt5_ticket,
                reason=reason
            )
            if created_at is not None:
                trade.created_at = created_at
            db.add(trade)
            db.commit()
            db.refresh(trade)
            return trade
        finally:
            db.close()

    @staticmethod
    def update_trailing_stop(trade_id: str, current_price: float) -> Optional[Trade]:
        """Update trailing stop as price moves in favorable direction."""
        db = SessionLocal()
        try:
            trade = db.query(Trade).filter(Trade.trade_id == trade_id).first()
            if not trade or trade.status != 'open' or not trade.trailing_stop_distance:
                return trade

            updated = False

            if trade.trade_type.lower() == 'call':
                # For CALL trades: if price goes higher, move stop up
                if current_price > (trade.highest_price_seen or 0):
                    trade.highest_price_seen = current_price
                    trade.trailing_stop_price = current_price - trade.trailing_stop_distance
                    updated = True
            else:
                # For PUT trades: if price goes lower, move stop down
                # highest_price_seen actually tracks lowest for PUT
                if trade.highest_price_seen is None or current_price < trade.highest_price_seen:
                    trade.highest_price_seen = current_price
                    trade.trailing_stop_price = current_price + trade.trailing_stop_distance
                    updated = True

            if updated:
                db.commit()
                db.refresh(trade)
            return trade
        finally:
            db.close()

    @staticmethod
    def get_trades_with_active_stops(user_id: str = None) -> List[Trade]:
        """Get all open trades that have active stop-loss, take-profit, or trailing stop settings."""
        db = SessionLocal()
        try:
            query = db.query(Trade).filter(
                Trade.status == 'open'
            ).filter(
                (Trade.stop_loss.isnot(None)) |
                (Trade.take_profit.isnot(None)) |
                (Trade.trailing_stop_price.isnot(None))
            )
            if user_id is not None:
                query = query.filter(Trade.user_id == user_id)
            return query.all()
        finally:
            db.close()