"""
Trade Monitor Service

A background service that polls the Deriv API to monitor open trades
and updates the database when contracts settle.
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Dict, Any, List, Optional, Set
from contextlib import asynccontextmanager
try:
    from websockets.protocol import State  # Deriv path only (BROKER=deriv)
except ModuleNotFoundError:  # pragma: no cover - MT5-only deployments
    State = None

from sqlalchemy import or_, and_

from deriv_client import DerivAPIClient
from database import Trade, SessionLocal, TradingRepository
from notify import notify_telegram

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TradeMonitorError(Exception):
    """Base exception for TradeMonitor errors."""
    pass


class TradeMonitor:
    """
    Background service for monitoring open trades and updating results.

    Polls the Deriv API periodically to check contract status and updates
    the database when contracts settle.
    """

    DEFAULT_POLL_INTERVAL = 30  # seconds
    MAX_RETRIES = 3
    RETRY_DELAY = 5  # seconds

    def __init__(
        self,
        deriv_client: Optional[DerivAPIClient] = None,
        poll_interval: int = DEFAULT_POLL_INTERVAL
    ):
        """
        Initialize the TradeMonitor.

        Args:
            deriv_client: Optional DerivAPIClient instance. If not provided,
                         a new one will be created.
            poll_interval: Interval in seconds between polling cycles.
        """
        self.broker = os.getenv("BROKER", "deriv").lower()
        if deriv_client is not None:
            self.deriv_client = deriv_client
        elif self.broker == "mt5":
            from mt5_client import MT5Client
            self.deriv_client = MT5Client()
        else:
            self.deriv_client = DerivAPIClient()
        self.poll_interval = poll_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._processed_contracts: Set[str] = set()
        # Locks to prevent race conditions during concurrent operations
        self._check_trades_lock = asyncio.Lock()
        self._check_stops_lock = asyncio.Lock()
        self._update_trade_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        """Check if the monitor is currently running."""
        return self._running

    async def start(self) -> None:
        """
        Start the background polling loop.

        Raises:
            TradeMonitorError: If the monitor is already running.
        """
        if self._running:
            raise TradeMonitorError("TradeMonitor is already running")

        self._running = True
        logger.info("Starting TradeMonitor with %d second poll interval", self.poll_interval)

        try:
            # Connect to the broker
            connected = await self.deriv_client.connect()
            if not connected:
                if self.broker == "mt5":
                    # Bridge may be momentarily busy; reads reconnect lazily.
                    logger.warning("MT5 initial connect failed; will retry on poll")
                else:
                    raise TradeMonitorError("Failed to connect to Deriv API")

            # Start the polling loop
            self._task = asyncio.create_task(self._polling_loop())
            logger.info("TradeMonitor started successfully")

        except Exception as e:
            self._running = False
            logger.error("Failed to start TradeMonitor: %s", e)
            raise TradeMonitorError(f"Failed to start TradeMonitor: {e}") from e

    async def _ensure_connected(self) -> bool:
        """
        Ensure WebSocket is connected, reconnect if needed.

        Returns:
            bool: True if connected, False if reconnection failed after retries.
        """
        if not self.deriv_client.websocket or self.deriv_client.websocket.state != State.OPEN:
            self.deriv_client.websocket = None
            logger.warning("WebSocket connection lost, attempting to reconnect...")

            for attempt in range(self.MAX_RETRIES):
                try:
                    connected = await self.deriv_client.connect()
                    if connected:
                        logger.info("Successfully reconnected to Deriv API")
                        return True
                    logger.warning("Reconnect attempt %d/%d failed", attempt + 1, self.MAX_RETRIES)
                    await asyncio.sleep(self.RETRY_DELAY)
                except Exception as e:
                    logger.warning("Reconnect attempt %d/%d raised exception: %s", attempt + 1, self.MAX_RETRIES, e)
                    await asyncio.sleep(self.RETRY_DELAY)

            logger.error("Failed to reconnect after %d attempts", self.MAX_RETRIES)
            return False

        return True

    async def stop(self) -> None:
        """
        Stop the polling loop gracefully.
        """
        if not self._running:
            logger.warning("TradeMonitor is not running")
            return

        logger.info("Stopping TradeMonitor...")
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # Disconnect from Deriv API
        try:
            await self.deriv_client.disconnect()
        except Exception as e:
            logger.warning("Error disconnecting from Deriv API: %s", e)

        logger.info("TradeMonitor stopped")

    async def _polling_loop(self) -> None:
        """
        Main polling loop that runs continuously.
        """
        while self._running:
            try:
                if self.broker == "mt5":
                    # MT5 enforces SL/TP server-side; just reconcile closures.
                    await self._reconcile_mt5()
                else:
                    # Ensure connection before polling
                    if not await self._ensure_connected():
                        logger.error("Lost connection to Deriv API, waiting before retry")
                        await asyncio.sleep(self.poll_interval)
                        continue

                    await self.check_open_trades()
                    # Also check stop levels for trades with SL/TP settings
                    await self.check_stop_levels()
                    # Backfill any rows missing entry/exit spot prices from Deriv
                    await self.reconcile_missing_prices()
            except Exception as e:
                logger.error("Error in polling loop: %s", e)

            # Wait for next poll interval
            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    async def _reconcile_mt5(self) -> Dict[str, int]:
        """MT5 settlement reconciliation. SL/TP are enforced server-side, so the
        monitor only needs to detect when a tracked open ticket has left
        positions_get() (closed) and settle it from history_deals_get()."""
        result = {"checked": 0, "closed": 0, "errors": 0}
        async with self._check_trades_lock:
            open_trades = [t for t in self._get_open_trades() if t.mt5_ticket]
            if not open_trades:
                return result
            try:
                positions = await self.deriv_client.get_open_positions()
            except Exception as e:
                logger.error("MT5 positions_get failed: %s", e)
                result["errors"] += 1
                return result
            pos_by_ticket = {int(p["ticket"]): p for p in positions}
            for t in open_trades:
                result["checked"] += 1
                p = pos_by_ticket.get(int(t.mt5_ticket))
                if p is not None:
                    # Still open — refresh live price + floating P&L so the
                    # read-only cluster backend can serve them to the TUI.
                    TradingRepository.update_live_price(
                        t.trade_id, p.get("current_price"), p.get("profit_loss"))
                    continue
                try:
                    close = await self.deriv_client.get_position_close(int(t.mt5_ticket))
                    if not close.get("closed"):
                        continue  # not yet resolvable; retry next cycle
                    await self._update_trade_result(t, {
                        "exit_price": close.get("exit_price"),
                        "profit_loss": close.get("profit"),
                        "closed_at": close.get("closed_at") or datetime.utcnow(),
                    })
                    result["closed"] += 1
                    try:
                        pnl = float(close.get("profit") or 0.0)
                        emoji = "🟢" if pnl >= 0 else "🔴"
                        outcome = "profit" if pnl >= 0 else "loss"
                        await asyncio.to_thread(
                            notify_telegram,
                            f"{emoji} Closed {t.symbol} {str(t.trade_type).upper()} "
                            f"#{t.mt5_ticket} | exit {close.get('exit_price')} | "
                            f"P&L ${pnl:+.2f} ({outcome})")
                    except Exception:
                        pass
                except Exception as e:
                    logger.error("MT5 reconcile error for ticket %s: %s", t.mt5_ticket, e)
                    result["errors"] += 1
        return result

    async def check_open_trades(self) -> Dict[str, Any]:
        """
        Check all open trades and update their status.

        Returns:
            Dict containing:
                - checked: Number of trades checked
                - updated: Number of trades updated
                - errors: Number of errors encountered
        """
        result = {
            "checked": 0,
            "updated": 0,
            "errors": 0,
            "details": []
        }

        # Acquire lock to prevent concurrent executions
        async with self._check_trades_lock:
            try:
                # Get all open trades from database
                open_trades = self._get_open_trades()
                result["checked"] = len(open_trades)

                if not open_trades:
                    logger.debug("No open trades to check")
                    return result

                logger.info("Checking %d open trades", len(open_trades))

                # Get current portfolio from Deriv API
                portfolio_response = await self._fetch_with_retry(
                    self.deriv_client.get_portfolio
                )

                # Get profit table for recently closed contracts
                profit_table_response = await self._fetch_with_retry(
                    self.deriv_client.get_profit_table
                )

                # Build lookup maps for faster processing
                active_contracts = self._build_active_contracts_map(portfolio_response)
                completed_contracts = self._build_completed_contracts_map(profit_table_response)

                # Check each open trade
                for trade in open_trades:
                    try:
                        update_result = await self._check_and_update_trade(
                            trade,
                            active_contracts,
                            completed_contracts
                        )

                        if update_result.get("updated"):
                            result["updated"] += 1
                            result["details"].append(update_result)

                    except Exception as e:
                        result["errors"] += 1
                        logger.error("Error checking trade %s: %s", trade.trade_id, e)

                logger.info(
                    "Trade check complete: %d checked, %d updated, %d errors",
                    result["checked"],
                    result["updated"],
                    result["errors"]
                )

            except Exception as e:
                logger.error("Error in check_open_trades: %s", e)
                raise

        return result

    async def _check_and_update_trade(
        self,
        trade: Trade,
        active_contracts: Dict[str, Dict[str, Any]],
        completed_contracts: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Check a single trade and update if settled.

        Args:
            trade: The Trade object to check.
            active_contracts: Map of active contract IDs to their data.
            completed_contracts: Map of completed contract IDs to their data.

        Returns:
            Dict with update status and details.
        """
        result = {"trade_id": trade.trade_id, "updated": False}

        contract_id = trade.trade_id

        # Prevent unbounded growth — safe to clear since it only guards
        # against double-processing within a polling cycle.
        if len(self._processed_contracts) > 1000:
            self._processed_contracts.clear()

        # Skip if already processed in this session
        if contract_id in self._processed_contracts:
            return result

        # Check if contract is still active in portfolio
        if contract_id in active_contracts:
            logger.debug("Trade %s is still active", contract_id)
            return result

        # Check if contract is in recently completed list
        if contract_id in completed_contracts:
            contract_data = completed_contracts[contract_id]
            update_data = self._extract_settlement_data(contract_data)
            await self._update_trade_result(trade, update_data)

            self._processed_contracts.add(contract_id)
            result["updated"] = True
            result["profit_loss"] = update_data.get("profit_loss")
            result["exit_price"] = update_data.get("exit_price")

            logger.info(
                "Trade %s settled (from profit table): P/L = %.2f",
                contract_id,
                update_data.get("profit_loss", 0)
            )
            return result

        # Contract not in portfolio or profit table - check directly via API
        try:
            contract_response = await self.deriv_client.get_contract_status(contract_id)

            if 'error' in contract_response:
                logger.warning(
                    "Could not get status for trade %s: %s",
                    contract_id,
                    contract_response['error'].get('message', 'Unknown error')
                )
                return result

            poc = contract_response.get('proposal_open_contract', {})

            # Check if contract is sold or expired
            is_sold = poc.get('is_sold', 0)
            is_expired = poc.get('is_expired', 0)

            if is_sold or is_expired:
                profit = poc.get('profit', 0)
                sell_price = poc.get('sell_price', 0)
                status = poc.get('status', 'unknown')

                update_data = {
                    "exit_price": sell_price,
                    "profit_loss": profit,
                    "closed_at": datetime.utcnow()
                }

                await self._update_trade_result(trade, update_data)

                self._processed_contracts.add(contract_id)
                result["updated"] = True
                result["profit_loss"] = profit
                result["exit_price"] = sell_price
                result["status"] = status

                logger.info(
                    "Trade %s settled (direct check): status=%s, P/L = %.2f",
                    contract_id,
                    status,
                    profit
                )
            else:
                logger.debug("Trade %s is still open (direct check)", contract_id)

        except Exception as e:
            logger.error("Error checking contract status for %s: %s", contract_id, e)

        return result

    async def _update_trade_result(
        self,
        trade: Trade,
        update_data: Dict[str, Any]
    ) -> None:
        """
        Update a single trade with settlement results.

        Args:
            trade: The Trade object to update.
            update_data: Dict containing exit_price, profit_loss, etc.
        """
        # Acquire lock to prevent concurrent database updates
        async with self._update_trade_lock:
            db = SessionLocal()
            try:
                # Fetch the trade fresh from the database
                db_trade = db.query(Trade).filter(Trade.id == trade.id).first()

                if not db_trade:
                    logger.error("Trade %s not found in database", trade.trade_id)
                    return

                if db_trade.status == "closed":
                    logger.debug("Trade %s is already closed", trade.trade_id)
                    return

                # Update the trade
                db_trade.exit_price = update_data.get("exit_price")
                db_trade.profit_loss = update_data.get("profit_loss")
                db_trade.status = "closed"
                db_trade.closed_at = update_data.get("closed_at", datetime.utcnow())

                db.commit()

                logger.info(
                    "Updated trade %s: exit_price=%.5f, profit_loss=%.2f",
                    trade.trade_id,
                    db_trade.exit_price or 0,
                    db_trade.profit_loss or 0
                )

            except Exception as e:
                db.rollback()
                logger.error("Failed to update trade %s: %s", trade.trade_id, e)
                raise
            finally:
                db.close()

    def _get_open_trades(self) -> List[Trade]:
        """
        Get all open trades from the database.

        Returns:
            List of Trade objects with status='open'.
        """
        db = SessionLocal()
        try:
            return db.query(Trade).filter(Trade.status == "open").all()
        finally:
            db.close()

    async def reconcile_missing_prices(self) -> Dict[str, int]:
        """Find trades with missing/zero entry_price (any status) or missing/zero
        exit_price (closed only), and backfill them from Deriv contract status.
        Idempotent — safe to call repeatedly."""
        db = SessionLocal()
        try:
            bad = db.query(Trade).filter(
                or_(
                    Trade.entry_price == 0,
                    Trade.entry_price.is_(None),
                    and_(
                        Trade.status == "closed",
                        or_(Trade.exit_price == 0, Trade.exit_price.is_(None)),
                    ),
                )
            ).all()
        finally:
            db.close()

        result = {"checked": len(bad), "fixed": 0, "errors": 0}
        if not bad:
            return result

        for t in bad:
            try:
                resp = await self.deriv_client.get_contract_status(str(t.trade_id))
                poc = (resp or {}).get("proposal_open_contract", {}) or {}
                entry = poc.get("entry_spot") or poc.get("entry_tick")
                exit_p = poc.get("exit_tick")
                profit = poc.get("profit")

                async with self._update_trade_lock:
                    db = SessionLocal()
                    try:
                        row = db.query(Trade).filter(Trade.id == t.id).first()
                        if not row:
                            continue
                        changed = False
                        if entry and (not row.entry_price or row.entry_price == 0):
                            row.entry_price = float(entry)
                            changed = True
                        if exit_p and (not row.exit_price or row.exit_price == 0):
                            row.exit_price = float(exit_p)
                            changed = True
                        if profit is not None and row.profit_loss is None:
                            row.profit_loss = float(profit)
                            changed = True
                        if changed:
                            db.commit()
                            result["fixed"] += 1
                            logger.info("Reconciled trade %s", t.trade_id)
                    finally:
                        db.close()
            except Exception as e:
                result["errors"] += 1
                logger.warning("Reconcile failed for %s: %s", t.trade_id, e)

        if result["fixed"]:
            logger.info("Price reconciliation: %d/%d fixed (%d errors)",
                        result["fixed"], result["checked"], result["errors"])
        return result

    async def _fetch_with_retry(
        self,
        fetch_func,
        max_retries: int = None,
        retry_delay: int = None
    ) -> Dict[str, Any]:
        """
        Fetch data with retry logic for resilience.

        Args:
            fetch_func: Async function to call.
            max_retries: Maximum number of retry attempts.
            retry_delay: Delay in seconds between retries.

        Returns:
            Response from the fetch function.

        Raises:
            TradeMonitorError: If all retries fail.
        """
        max_retries = max_retries or self.MAX_RETRIES
        retry_delay = retry_delay or self.RETRY_DELAY

        last_error = None

        for attempt in range(max_retries):
            try:
                response = await fetch_func()

                # Check for API errors
                if "error" in response:
                    error_msg = response["error"].get("message", "Unknown API error")
                    raise TradeMonitorError(f"Deriv API error: {error_msg}")

                return response

            except TradeMonitorError:
                raise
            except Exception as e:
                last_error = e
                logger.warning(
                    "Fetch attempt %d/%d failed: %s",
                    attempt + 1,
                    max_retries,
                    e
                )

                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    # Try to reconnect
                    try:
                        await self.deriv_client.connect()
                    except Exception:
                        pass

        raise TradeMonitorError(
            f"All {max_retries} fetch attempts failed: {last_error}"
        )

    def _build_active_contracts_map(
        self,
        portfolio_response: Dict[str, Any]
    ) -> Dict[str, Dict[str, Any]]:
        """
        Build a map of active contract IDs to their data.

        Args:
            portfolio_response: Response from get_portfolio().

        Returns:
            Dict mapping contract_id to contract data.
        """
        contracts = {}

        portfolio = portfolio_response.get("portfolio", {})
        contract_list = portfolio.get("contracts", [])

        for contract in contract_list:
            contract_id = str(contract.get("contract_id", ""))
            if contract_id:
                contracts[contract_id] = contract

        return contracts

    def _build_completed_contracts_map(
        self,
        profit_table_response: Dict[str, Any]
    ) -> Dict[str, Dict[str, Any]]:
        """
        Build a map of completed contract IDs to their settlement data.

        Args:
            profit_table_response: Response from get_profit_table().

        Returns:
            Dict mapping contract_id to settlement data.
        """
        contracts = {}

        profit_table = profit_table_response.get("profit_table", {})
        transactions = profit_table.get("transactions", [])

        for transaction in transactions:
            contract_id = str(transaction.get("contract_id", ""))
            if contract_id:
                contracts[contract_id] = transaction

        return contracts

    def _extract_settlement_data(
        self,
        contract_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Extract settlement data from a completed contract.

        Args:
            contract_data: Contract data from profit table.

        Returns:
            Dict with exit_price, profit_loss, closed_at.
        """
        # Extract profit/loss (USD) — sell_price/buy_price are USD amounts.
        profit_loss = contract_data.get("sell_price", 0) - contract_data.get("buy_price", 0)

        # exit_price must be a SPOT price (matching entry_price). The profit_table
        # only carries USD sell_price, not spot. Leave 0 here; the reconciliation
        # sweep will fill exit_tick from proposal_open_contract on the next pass.
        exit_price = 0.0

        # Extract sell time as closed_at
        sell_time = contract_data.get("sell_time")
        if sell_time:
            closed_at = datetime.utcfromtimestamp(sell_time)
        else:
            closed_at = datetime.utcnow()

        return {
            "exit_price": exit_price,
            "profit_loss": profit_loss,
            "closed_at": closed_at
        }

    async def check_stop_levels(self) -> Dict[str, Any]:
        """
        Check all trades with active stop levels and trigger sells when needed.

        Returns:
            Dict containing check results.
        """
        result = {
            "checked": 0,
            "triggered": 0,
            "trailing_updates": 0,
            "errors": 0,
            "details": []
        }

        # Acquire lock to prevent concurrent stop level checks
        async with self._check_stops_lock:
            try:
                # Get all trades with active stops
                trades_with_stops = TradingRepository.get_trades_with_active_stops()
                result["checked"] = len(trades_with_stops)

                if not trades_with_stops:
                    logger.debug("No trades with active stop levels")
                    return result

                logger.info("Checking %d trades with stop levels", len(trades_with_stops))

                # Group trades by symbol to minimize API calls
                symbols = set(trade.symbol for trade in trades_with_stops)

                # Fetch current prices for each symbol
                symbol_prices = {}
                for symbol in symbols:
                    try:
                        response = await self.deriv_client.get_ticks(symbol)
                        if 'tick' in response:
                            symbol_prices[symbol] = float(response['tick'].get('quote', 0))
                    except Exception as e:
                        logger.error("Failed to get price for %s: %s", symbol, e)

                # Check each trade against its stop levels
                for trade in trades_with_stops:
                    try:
                        current_price = symbol_prices.get(trade.symbol)
                        if current_price is None:
                            continue

                        check_result = await self._check_trade_stops(trade, current_price)

                        if check_result.get("triggered"):
                            result["triggered"] += 1
                            result["details"].append(check_result)
                        elif check_result.get("trailing_updated"):
                            result["trailing_updates"] += 1

                    except Exception as e:
                        result["errors"] += 1
                        logger.error("Error checking stops for trade %s: %s", trade.trade_id, e)

                logger.info(
                    "Stop level check complete: %d checked, %d triggered, %d trailing updates, %d errors",
                    result["checked"],
                    result["triggered"],
                    result["trailing_updates"],
                    result["errors"]
                )

            except Exception as e:
                logger.error("Error in check_stop_levels: %s", e)
                raise

        return result

    async def _check_trade_stops(
        self,
        trade: Trade,
        current_price: float
    ) -> Dict[str, Any]:
        """
        Check a single trade's stop levels and trigger sell if needed.

        Args:
            trade: The Trade object to check.
            current_price: Current market price for the symbol.

        Returns:
            Dict with check status and any actions taken.
        """
        result = {
            "trade_id": trade.trade_id,
            "triggered": False,
            "trigger_type": None,
            "trailing_updated": False
        }

        is_call = trade.trade_type.lower() == 'call'

        # Check trailing stop first (update if price moved favorably)
        if trade.trailing_stop_distance:
            updated_trade = TradingRepository.update_trailing_stop(trade.trade_id, current_price)
            if updated_trade and updated_trade.trailing_stop_price != trade.trailing_stop_price:
                result["trailing_updated"] = True
                trade = updated_trade  # Use updated trade for stop checks
                logger.debug(
                    "Trade %s trailing stop updated to %.5f",
                    trade.trade_id,
                    trade.trailing_stop_price
                )

        # Check if any stop level is triggered
        should_sell = False
        trigger_type = None

        # Stop-loss check
        if trade.stop_loss:
            if is_call and current_price <= trade.stop_loss:
                should_sell = True
                trigger_type = "stop_loss"
                logger.info(
                    "CALL trade %s stop-loss triggered: price %.5f <= SL %.5f",
                    trade.trade_id,
                    current_price,
                    trade.stop_loss
                )
            elif not is_call and current_price >= trade.stop_loss:
                should_sell = True
                trigger_type = "stop_loss"
                logger.info(
                    "PUT trade %s stop-loss triggered: price %.5f >= SL %.5f",
                    trade.trade_id,
                    current_price,
                    trade.stop_loss
                )

        # Take-profit check
        if not should_sell and trade.take_profit:
            if is_call and current_price >= trade.take_profit:
                should_sell = True
                trigger_type = "take_profit"
                logger.info(
                    "CALL trade %s take-profit triggered: price %.5f >= TP %.5f",
                    trade.trade_id,
                    current_price,
                    trade.take_profit
                )
            elif not is_call and current_price <= trade.take_profit:
                should_sell = True
                trigger_type = "take_profit"
                logger.info(
                    "PUT trade %s take-profit triggered: price %.5f <= TP %.5f",
                    trade.trade_id,
                    current_price,
                    trade.take_profit
                )

        # Trailing stop check
        if not should_sell and trade.trailing_stop_price:
            if is_call and current_price <= trade.trailing_stop_price:
                should_sell = True
                trigger_type = "trailing_stop"
                logger.info(
                    "CALL trade %s trailing stop triggered: price %.5f <= TSL %.5f",
                    trade.trade_id,
                    current_price,
                    trade.trailing_stop_price
                )
            elif not is_call and current_price >= trade.trailing_stop_price:
                should_sell = True
                trigger_type = "trailing_stop"
                logger.info(
                    "PUT trade %s trailing stop triggered: price %.5f >= TSL %.5f",
                    trade.trade_id,
                    current_price,
                    trade.trailing_stop_price
                )

        if should_sell:
            sell_result = await self._execute_stop_sell(trade, trigger_type, current_price)
            result["triggered"] = True
            result["trigger_type"] = trigger_type
            result["sell_result"] = sell_result

        return result

    async def _execute_stop_sell(
        self,
        trade: Trade,
        trigger_type: str,
        current_price: float
    ) -> Dict[str, Any]:
        """
        Execute a sell when a stop level is triggered.

        Args:
            trade: The Trade object to sell.
            trigger_type: Type of stop that was triggered (stop_loss, take_profit, trailing_stop).
            current_price: Current market price.

        Returns:
            Dict with sell result details.
        """
        result = {
            "success": False,
            "trade_id": trade.trade_id,
            "trigger_type": trigger_type,
            "current_price": current_price
        }

        try:
            # Execute sell via Deriv API
            response = await self.deriv_client.sell_contract(trade.trade_id)

            if 'error' in response:
                error_msg = response['error'].get('message', 'Unknown error')
                logger.error(
                    "Failed to sell trade %s (%s): %s",
                    trade.trade_id,
                    trigger_type,
                    error_msg
                )
                result["error"] = error_msg
                return result

            sell_data = response.get('sell', {})
            sold_for = float(sell_data.get('sold_for', 0))
            profit_loss = sold_for - trade.amount

            # Pull spot exit_tick (not the USD sold_for) so exit_price is consistent
            # with entry_price across all close paths.
            spot_exit_price = 0.0
            try:
                status_resp = await self.deriv_client.get_contract_status(str(trade.trade_id))
                poc = status_resp.get('proposal_open_contract', {}) or {}
                spot_exit_price = float(poc.get('exit_tick') or poc.get('current_spot') or 0)
            except Exception:
                pass

            # Update trade in database
            TradingRepository.update_trade_result(
                trade_id=trade.trade_id,
                exit_price=spot_exit_price,
                profit_loss=profit_loss,
                status='closed'
            )

            result["success"] = True
            result["sold_for"] = sold_for
            result["profit_loss"] = profit_loss

            logger.info(
                "Trade %s sold via %s: sold_for=%.2f, P/L=%.2f",
                trade.trade_id,
                trigger_type,
                sold_for,
                profit_loss
            )

        except Exception as e:
            logger.error("Error executing stop sell for trade %s: %s", trade.trade_id, e)
            result["error"] = str(e)

        return result

    @asynccontextmanager
    async def running(self):
        """
        Context manager for running the trade monitor.

        Usage:
            async with trade_monitor.running():
                # Monitor is running here
                await some_async_operation()
        """
        await self.start()
        try:
            yield self
        finally:
            await self.stop()


async def run_trade_monitor(poll_interval: int = 30) -> None:
    """
    Run the trade monitor as a standalone service.

    Args:
        poll_interval: Interval in seconds between polls.
    """
    monitor = TradeMonitor(poll_interval=poll_interval)

    try:
        async with monitor.running():
            # Keep running until interrupted
            while True:
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info("Trade monitor cancelled")
    except KeyboardInterrupt:
        logger.info("Trade monitor interrupted")


# Allow running as a standalone script
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Trade Monitor Service")
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Polling interval in seconds (default: 30)"
    )

    args = parser.parse_args()

    try:
        asyncio.run(run_trade_monitor(poll_interval=args.interval))
    except KeyboardInterrupt:
        print("\nShutting down...")
