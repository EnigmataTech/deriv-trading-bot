import os
from dotenv import load_dotenv
load_dotenv()
from sqlalchemy import create_engine, Column, Integer, String, Text, Float, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from typing import List, Optional
from datetime import datetime


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////app/data/trading_database.db")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def migrate_database():
    """Add new columns to existing database if they don't exist."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if 'trades' not in inspector.get_table_names():
        return  # Table doesn't exist yet, will be created by create_all

    existing_columns = [col['name'] for col in inspector.get_columns('trades')]
    new_columns = {
        'stop_loss': 'FLOAT',
        'take_profit': 'FLOAT',
        'trailing_stop_distance': 'FLOAT',
        'trailing_stop_price': 'FLOAT',
        'highest_price_seen': 'FLOAT'
    }

    with engine.connect() as conn:
        for col_name, col_type in new_columns.items():
            if col_name not in existing_columns:
                conn.execute(text(f'ALTER TABLE trades ADD COLUMN {col_name} {col_type}'))
                conn.commit()


# Run migration before create_all
migrate_database()

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

class Portfolio(Base):
    __tablename__ = "portfolios"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, nullable=False, index=True)
    balance = Column(Float, nullable=False, default=0.0)
    equity = Column(Float, nullable=False, default=0.0)
    margin = Column(Float, nullable=False, default=0.0)
    free_margin = Column(Float, nullable=False, default=0.0)
    updated_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

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
    def update_portfolio(user_id: str, balance: float, equity: float, margin: float, free_margin: float) -> Portfolio:
        db = SessionLocal()
        try:
            portfolio = db.query(Portfolio).filter(Portfolio.user_id == user_id).first()
            if portfolio:
                portfolio.balance = balance
                portfolio.equity = equity
                portfolio.margin = margin
                portfolio.free_margin = free_margin
                portfolio.updated_at = datetime.utcnow()
            else:
                portfolio = Portfolio(
                    user_id=user_id,
                    balance=balance,
                    equity=equity,
                    margin=margin,
                    free_margin=free_margin
                )
                db.add(portfolio)
            db.commit()
            db.refresh(portfolio)
            return portfolio
        finally:
            db.close()

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
        """Update a trade with its final result. Set closed_at to current time."""
        db = SessionLocal()
        try:
            trade = db.query(Trade).filter(Trade.trade_id == trade_id).first()
            if trade:
                trade.exit_price = exit_price
                trade.profit_loss = profit_loss
                trade.status = status
                trade.closed_at = datetime.utcnow()
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
        trailing_stop_distance: Optional[float] = None
    ) -> Trade:
        """Create a trade with optional SL/TP settings."""
        db = SessionLocal()
        try:
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
                highest_price_seen=highest_price_seen
            )
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