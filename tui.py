"""Deriv Trading Bot — Rich TUI Dashboard

Run: python tui.py
"""

import asyncio
import logging
import sys
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from websockets.protocol import State
from rich.text import Text

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Rule,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
)
from textual import work

from deriv_client import DerivAPIClient, TechnicalIndicators
from database import TradingRepository
from trade_monitor import TradeMonitor
from logger import setup_logging
from symbols import get_short_name

load_dotenv()

TUI_USER_ID = "local_user"
DEFAULT_SYMBOLS = ["R_50", "R_100", "1HZ50V", "1HZ100V"]


# ─── Trade Detail Modal ───────────────────────────────────────────────────────

class TradeDetailModal(ModalScreen):
    """Modal overlay showing full details of a single trade."""

    CSS = """
    TradeDetailModal {
        align: center middle;
    }

    #modal-container {
        width: 64;
        height: auto;
        max-height: 80vh;
        background: $surface;
        border: double $primary;
        padding: 1 2;
    }

    #modal-title {
        text-align: center;
        text-style: bold;
        padding-bottom: 1;
        color: $accent;
    }

    .detail-row {
        height: 1;
        margin-bottom: 0;
    }

    .detail-label {
        width: 22;
        color: $text-muted;
    }

    .detail-value {
        width: 1fr;
        color: $text;
    }

    #modal-hint {
        text-align: center;
        color: $text-muted;
        padding-top: 1;
        text-style: italic;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("enter", "dismiss", "Close", show=False),
    ]

    def __init__(self, trade) -> None:
        super().__init__()
        self._trade = trade

    def compose(self) -> ComposeResult:
        t = self._trade
        pnl = t.profit_loss
        if pnl is not None:
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        else:
            pnl_str = "—"

        rows = [
            ("Trade ID",        str(t.trade_id)),
            ("Symbol",          t.symbol),
            ("Type",            t.trade_type.upper()),
            ("Amount",          f"${t.amount:.2f}"),
            ("Entry Price",     f"{t.entry_price:.5f}"),
            ("P&L",             pnl_str),
            ("Status",          t.status.upper()),
            ("Stop Loss",       f"{t.stop_loss:.5f}" if t.stop_loss else "—"),
            ("Take Profit",     f"{t.take_profit:.5f}" if t.take_profit else "—"),
            ("Trailing Dist.",  f"{t.trailing_stop_distance:.5f}" if t.trailing_stop_distance else "—"),
            ("Trailing Price",  f"{t.trailing_stop_price:.5f}" if t.trailing_stop_price else "—"),
            ("Opened",          t.created_at.strftime("%Y-%m-%d %H:%M:%S") if t.created_at else "—"),
            ("Closed",          t.closed_at.strftime("%Y-%m-%d %H:%M:%S") if t.closed_at else "—"),
        ]

        with Vertical(id="modal-container"):
            yield Static("── TRADE DETAIL ──", id="modal-title")
            yield Rule()
            for label, value in rows:
                with Horizontal(classes="detail-row"):
                    yield Static(f"{label}:", classes="detail-label")
                    yield Static(value, classes="detail-value")
            yield Rule()
            yield Static("Press [bold]Enter[/bold] or [bold]Esc[/bold] to close", id="modal-hint")


# ─── Main Application ─────────────────────────────────────────────────────────

class DerivTradingApp(App):
    """Deriv Trading Bot — Terminal UI."""

    CSS = """
    Screen { background: $surface; }

    #dashboard { height: 1fr; }

    .panel {
        border: solid $primary;
        padding: 0 1;
        margin: 0 1 1 0;
    }

    #account-panel   { width: 32; height: 6; }
    #portfolio-panel { width: 1fr; height: 6; }
    #status-panel    { width: 22; height: 6; }
    #top-row         { height: 6; }
    #market-table    { height: 9; }
    #sparkline-panel { height: auto; min-height: 4; max-height: 8; }
    #open-trades-table { height: 9; }
    #activity-log    { height: 1fr; min-height: 6; }

    .sparkline-row    { height: 1; }
    .sparkline-label  { width: 10; color: $text-muted; }
    .sparkline-widget { width: 1fr; height: 1; }

    #command-bar {
        dock: bottom;
        height: 3;
        border-top: solid $primary;
    }

    #history-table { height: 1fr; }

    .form-row    { height: 3; margin-bottom: 1; }
    .form-label  { width: 16; content-align: right middle; padding-right: 1; }
    Button       { margin-top: 1; }
    #place-log   { height: 8; border: solid $primary; }
    """

    BINDINGS = [
        Binding("1", "switch_tab('tab-dashboard')", "Dashboard"),
        Binding("2", "switch_tab('tab-history')", "History"),
        Binding("3", "switch_tab('tab-place')", "Place Trade"),
        Binding("j", "scroll_down_table", "Scroll Down", show=False),
        Binding("k", "scroll_up_table", "Scroll Up", show=False),
        Binding("escape", "focus_command_bar", "Command Bar", show=False),
        Binding("r", "refresh_all", "Refresh"),
        Binding("a", "toggle_auto_refresh", "Auto-Refresh"),
        Binding("b", "quick_buy", "Quick Buy"),
        Binding("s", "quick_sell", "Quick Sell"),
        Binding("ctrl+l", "clear_log", "Clear Log"),
        Binding("q", "quit", "Quit"),
        Binding("f1", "toggle_help", "Help"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._deriv = DerivAPIClient()
        self._api_lock: asyncio.Lock = asyncio.Lock()
        self._monitor: Optional[TradeMonitor] = None
        self._watched_symbols: list[str] = list(DEFAULT_SYMBOLS)
        # New state
        self._price_history: dict[str, list[float]] = {}
        self._auto_refresh: bool = True
        self._timer_balance: Optional[Timer] = None
        self._timer_open: Optional[Timer] = None
        self._timer_market: Optional[Timer] = None
        self._timer_history: Optional[Timer] = None
        self._cached_balance: Optional[float] = None
        self._cached_currency: Optional[str] = None
        self._last_open_count: int = 0
        self._last_history_count: int = 0
        self._last_refresh: dict[str, str] = {}
        self._open_trade_row_map: dict[str, object] = {}

    # ─── Layout ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="tabs"):
            with TabPane("Dashboard", id="tab-dashboard"):
                with VerticalScroll(id="dashboard"):
                    with Horizontal(id="top-row"):
                        yield Static("Balance: Loading...\nCurrency: —", id="account-panel", classes="panel")
                        yield Static("", id="portfolio-panel", classes="panel")
                        yield Static("", id="status-panel", classes="panel")
                    yield DataTable(id="market-table", classes="panel", cursor_type="row")
                    with Vertical(id="sparkline-panel", classes="panel"):
                        for sym in DEFAULT_SYMBOLS:
                            with Horizontal(classes="sparkline-row"):
                                yield Static(sym, classes="sparkline-label")
                                yield Sparkline(
                                    [],
                                    id=f"sparkline-{sym}",
                                    classes="sparkline-widget",
                                    min_color="#cc4444",
                                    max_color="#44cc44",
                                )
                    yield DataTable(id="open-trades-table", classes="panel", cursor_type="row")
                    yield RichLog(id="activity-log", classes="panel", markup=True)
            with TabPane("Trade History", id="tab-history"):
                yield DataTable(id="history-table")
            with TabPane("Place Trade", id="tab-place"):
                with Vertical():
                    yield Static("[bold]Quick Trade:[/bold]", classes="section-label")
                    with Horizontal(classes="quick-trade-buttons"):
                        yield Button("CALL R_50 $1", id="quick-call-r50", variant="success")
                        yield Button("PUT R_50 $1", id="quick-put-r50", variant="error")
                        yield Button("CALL R_100 $1", id="quick-call-r100", variant="success")
                        yield Button("PUT R_100 $1", id="quick-put-r100", variant="error")
                    yield Rule()
                    yield Static("[bold]Custom Trade:[/bold]", classes="section-label")
                    with Horizontal(classes="form-row"):
                        yield Label("Symbol:", classes="form-label")
                        yield Input(placeholder="e.g. R_50", id="pt-symbol", value="R_50")
                    with Horizontal(classes="form-row"):
                        yield Label("Direction:", classes="form-label")
                        yield Input(placeholder="CALL or PUT", id="pt-direction", value="CALL")
                    with Horizontal(classes="form-row"):
                        yield Label("Amount ($):", classes="form-label")
                        yield Input(placeholder="e.g. 1.00", id="pt-amount", value="1.00")
                    with Horizontal(classes="form-row"):
                        yield Label("Duration (t):", classes="form-label")
                        yield Input(placeholder="ticks", id="pt-duration", value="5")
                    with Horizontal(classes="form-row"):
                        yield Label("Stop Loss:", classes="form-label")
                        yield Input(placeholder="optional price", id="pt-sl")
                    with Horizontal(classes="form-row"):
                        yield Label("Take Profit:", classes="form-label")
                        yield Input(placeholder="optional price", id="pt-tp")
                    yield Button("Place Custom Trade", id="btn-place", variant="primary")
                    yield RichLog(id="place-log", markup=True)
        yield Input(placeholder="> command (type 'help')", id="command-bar")
        yield Footer()

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        mt = self.query_one("#market-table", DataTable)
        mt.add_columns("Symbol", "Price", "Change", "RSI(14)", "SMA(14)")
        mt.border_title = "MARKET DATA"

        ot = self.query_one("#open-trades-table", DataTable)
        ot.add_columns("Trade ID", "Symbol", "Type", "Amount", "Entry", "Current", "P&L")
        ot.border_title = "OPEN TRADES  [dim](Enter for detail)[/dim]"

        ht = self.query_one("#history-table", DataTable)
        ht.add_columns("Trade ID", "Symbol", "Type", "Amount", "Entry", "Exit", "P&L", "Closed")
        ht.border_title = "TRADE HISTORY"

        self.query_one("#account-panel", Static).border_title = "ACCOUNT"
        self.query_one("#portfolio-panel", Static).border_title = "PORTFOLIO STATS"
        self.query_one("#status-panel", Static).border_title = "CONNECTION"
        self.query_one("#sparkline-panel", Vertical).border_title = "PRICE SPARKLINES"
        self.query_one("#activity-log", RichLog).border_title = "LOG"

        self._set_connection_status(connected=False)

        # Initialize connection and fetch data before starting timers
        self._initialize_app()

    @work(exclusive=True)
    async def _initialize_app(self) -> None:
        """Initialize app: connect, fetch initial data, then start timers"""
        try:
            # Ensure connection is established first
            async with self._api_lock:
                if not self._deriv.websocket or self._deriv.websocket.state != State.OPEN:
                    self._deriv.websocket = None
                    connected = await self._deriv.connect()
                    if not connected:
                        self._log("[red]Failed to connect to Deriv API[/red]")
                        self._set_connection_status(connected=False)
                        return

            self._log("[green]Connected to Deriv API[/green]")
            self._set_connection_status(connected=True)

            # Fetch initial data in sequence (call internal async methods)
            await self._fetch_balance()
            await self._fetch_open_trades()
            await self._fetch_market_data()

            # Now start the periodic timers
            self._timer_balance = self.set_interval(30, self.refresh_balance)
            self._timer_open    = self.set_interval(3, self.refresh_open_trades)  # 3s for real-time P&L
            self._timer_market  = self.set_interval(5,  self.refresh_market_data)
            self._timer_history = self.set_interval(10, self.refresh_history)  # 10s for history updates

            # Start trade monitor
            self._start_monitor_worker()

        except Exception as e:
            self._log(f"[red]Initialization error: {e}[/red]")
            self._set_connection_status(connected=False)

    @work(exclusive=True)
    async def _start_monitor_worker(self) -> None:
        try:
            self._monitor = TradeMonitor(poll_interval=30)
            await self._monitor.start()
            self._log("[green]TradeMonitor started[/green]")
        except Exception as e:
            self._log(f"[yellow]TradeMonitor unavailable: {e}[/yellow]")

    async def on_unmount(self) -> None:
        if self._monitor and self._monitor.is_running:
            await self._monitor.stop()
        try:
            await self._deriv.disconnect()
        except Exception:
            pass

    # ─── Background workers ───────────────────────────────────────────────────

    async def _fetch_balance(self) -> None:
        """Internal method to fetch balance (can be awaited)"""
        try:
            async with self._api_lock:
                if not self._deriv.websocket or self._deriv.websocket.state != State.OPEN:
                    self._deriv.websocket = None
                    await self._deriv.connect()
                resp = await self._deriv.get_account_balance()
            if "error" in resp:
                self._log(f"[red]Balance: {resp['error'].get('message')}[/red]")
                self._set_connection_status(connected=False)
                return

            data = resp.get("balance", {})
            balance = data.get("balance", 0.0)
            currency = data.get("currency", "USD")

            # Update balance with smart caching
            if balance > 0:
                # If we have a cached balance and it's different, log the change
                if self._cached_balance is not None and abs(balance - self._cached_balance) > 0.01:
                    diff = balance - self._cached_balance
                    sign = "+" if diff >= 0 else ""
                    self._log(f"[bold cyan]Balance changed: {sign}${diff:,.2f} → ${balance:,.2f}[/bold cyan]")

                # Update cache and display
                self._cached_balance = balance
                self._cached_currency = currency
                self.query_one("#account-panel", Static).update(
                    f"Balance: ${balance:,.2f}\nCurrency: {currency}"
                )
                self._set_connection_status(connected=True)
            elif balance == 0 and self._cached_balance is None:
                # First load with 0 balance - accept it
                self._cached_balance = balance
                self._cached_currency = currency
                self.query_one("#account-panel", Static).update(
                    f"Balance: ${balance:,.2f}\nCurrency: {currency}"
                )
            elif self._cached_balance is not None:
                # API returned 0 or invalid, keep cached value
                # Don't log this every time to reduce noise
                pass

        except Exception as e:
            self._log(f"[red]Balance error: {e}[/red]")
            self._set_connection_status(connected=False)
            # Keep displaying cached balance if we have one
            if self._cached_balance is not None:
                self.query_one("#account-panel", Static).update(
                    f"Balance: ${self._cached_balance:,.2f}\nCurrency: {self._cached_currency}"
                )

    @work(exclusive=True)
    async def refresh_balance(self) -> None:
        """Worker wrapper for balance refresh"""
        await self._fetch_balance()

    async def _fetch_open_trades(self) -> None:
        """Internal method to fetch open trades (can be awaited)"""
        try:
            trades = TradingRepository.get_open_trades(TUI_USER_ID)
            # Only log if count changed
            if len(trades) != self._last_open_count:
                self._log(f"[cyan]Open trades count: {self._last_open_count} → {len(trades)}[/cyan]")
                self._last_open_count = len(trades)

            # Fetch live P&L + current prices + auto-close settled contracts
            live_pnl: dict[str, float] = {}
            live_prices: dict[str, float] = {}
            closed_count = 0

            try:
                async with self._api_lock:
                    if not self._deriv.websocket or self._deriv.websocket.state != State.OPEN:
                        self._deriv.websocket = None
                        await self._deriv.connect()
                    for t in trades:
                        try:
                            resp = await self._deriv.get_contract_status(str(t.trade_id))
                            poc = resp.get("proposal_open_contract", {})
                            profit = poc.get("profit")
                            current_spot = poc.get("current_spot")

                            if profit is not None:
                                live_pnl[str(t.trade_id)] = float(profit)
                            if current_spot is not None:
                                live_prices[str(t.trade_id)] = float(current_spot)

                            # Auto-close settled contracts
                            if poc.get("is_sold") or poc.get("status") == "sold":
                                exit_price = float(poc.get("exit_tick") or poc.get("current_spot") or 0)
                                final_pnl = float(poc.get("profit", 0))
                                TradingRepository.update_trade_result(
                                    str(t.trade_id), exit_price, final_pnl  # Fixed: correct parameter order
                                )
                                closed_count += 1
                                pnl_sign = "+" if final_pnl >= 0 else ""
                                status_icon = "✅" if final_pnl >= 0 else "❌"
                                close_msg = f"{status_icon} Trade #{t.trade_id} closed: {pnl_sign}${final_pnl:.2f}"
                                self._log(f"[bold cyan]{close_msg}[/bold cyan]")
                                severity = "information" if final_pnl >= 0 else "warning"
                                self.notify(close_msg, severity=severity, timeout=4)
                                # Force balance and history refresh when trade closes
                                self.refresh_balance()
                                self.refresh_history()
                        except Exception as e:
                            # Log individual trade fetch errors
                            pass
            except Exception as e:
                self._log(f"[yellow]P&L fetch error: {e}[/yellow]")

            # Re-fetch to exclude any that just auto-closed
            if closed_count > 0:
                self._log(f"[cyan]Removed {closed_count} closed trade(s) from open trades panel[/cyan]")

            trades = TradingRepository.get_open_trades(TUI_USER_ID)
            table = self.query_one("#open-trades-table", DataTable)
            table.clear()  # Clear all rows
            self._open_trade_row_map.clear()  # Clear the mapping

            if not trades:
                # Show helpful message when no open trades
                table.add_row("—", "No open trades", "—", "—", "—", "—", "—", key="empty")
            else:
                for t in trades:
                    pnl = live_pnl.get(str(t.trade_id), t.profit_loss)
                    current_price = live_prices.get(str(t.trade_id))

                    if pnl is not None:
                        pnl_text = (
                            Text(f"+${pnl:.2f}", style="bold green")
                            if pnl >= 0
                            else Text(f"-${abs(pnl):.2f}", style="bold red")
                        )
                    else:
                        pnl_text = Text("—")

                    type_text = (
                        Text("CALL", style="bold green")
                        if t.trade_type.upper() == "CALL"
                        else Text("PUT", style="bold red")
                    )

                    current_str = f"{current_price:.5f}" if current_price else "—"

                    row_key = str(t.trade_id)
                    symbol_display = f"{t.symbol} ({get_short_name(t.symbol)})"
                    table.add_row(
                        str(t.trade_id), symbol_display, type_text,
                        f"${t.amount:.2f}", f"{t.entry_price:.5f}", current_str, pnl_text,
                        key=row_key,
                    )
                    self._open_trade_row_map[row_key] = t

            stats = TradingRepository.get_trades_summary(TUI_USER_ID)
            pnl_total = stats["total_profit_loss"]
            sign = "+" if pnl_total >= 0 else ""
            self.query_one("#portfolio-panel", Static).update(
                f"Trades: {stats['total_trades']}  Win Rate: {stats['win_rate']:.1f}%\n"
                f"Open: {stats['open_trades']}  Total P&L: {sign}${pnl_total:.2f}"
            )
            self._last_refresh["open"] = datetime.now().strftime("%H:%M:%S")
            self._update_sub_title()
        except Exception as e:
            self._log(f"[red]Trades refresh error: {e}[/red]")
            import traceback
            self._log(f"[red]{traceback.format_exc()}[/red]")

    @work(exclusive=True)
    async def refresh_open_trades(self) -> None:
        """Worker wrapper for open trades refresh"""
        await self._fetch_open_trades()

    async def _fetch_market_data(self) -> None:
        """Internal method to fetch market data (can be awaited)"""
        table = self.query_one("#market-table", DataTable)
        table.clear()
        for symbol in list(self._watched_symbols):
            try:
                async with self._api_lock:
                    hist_resp = await self._deriv.get_ticks_history(symbol, count=50)
                prices = [float(p) for p in hist_resp.get("history", {}).get("prices", [])]
                symbol_display = f"{symbol} ({get_short_name(symbol)})"
                if not prices:
                    table.add_row(symbol_display, "NO DATA", "—", "—", "—")
                    continue

                price = prices[-1]
                change_cell = Text("—")
                rsi_cell = Text("—")
                sma_str = "—"

                if len(prices) >= 2:
                    change = prices[-1] - prices[-2]
                    if change >= 0:
                        change_cell = Text(f"▲ +{change:.5f}", style="bold green")
                    else:
                        change_cell = Text(f"▼ {change:.5f}", style="bold red")

                if len(prices) >= 15:
                    rsi_list = TechnicalIndicators.calculate_rsi(prices)
                    sma_list = TechnicalIndicators.calculate_sma(prices, 14)
                    if rsi_list and rsi_list[-1] is not None:
                        rsi_val = rsi_list[-1]
                        if rsi_val > 70:
                            rsi_cell = Text(f"{rsi_val:.1f}", style="bold red")
                        elif rsi_val < 30:
                            rsi_cell = Text(f"{rsi_val:.1f}", style="bold green")
                        else:
                            rsi_cell = Text(f"{rsi_val:.1f}", style="yellow")
                    if sma_list and sma_list[-1] is not None:
                        sma_str = f"{sma_list[-1]:.5f}"

                table.add_row(symbol_display, f"{price:.5f}", change_cell, rsi_cell, sma_str)

                # Update sparkline if panel exists for this symbol
                self._price_history[symbol] = prices[-20:]
                try:
                    sparkline = self.query_one(f"#sparkline-{symbol}", Sparkline)
                    sparkline.data = prices[-20:]
                except Exception:
                    pass

            except Exception as e:
                symbol_display = f"{symbol} ({get_short_name(symbol)})"
                table.add_row(symbol_display, "ERROR", "—", "—", "—")
                self._log(f"[red]Market data error for {symbol}: {e}[/red]")

        self._last_refresh["market"] = datetime.now().strftime("%H:%M:%S")
        self._update_sub_title()

    @work(exclusive=True)
    async def refresh_market_data(self) -> None:
        """Worker wrapper for market data refresh"""
        await self._fetch_market_data()

    @work(exclusive=True)
    async def refresh_history(self) -> None:
        try:
            all_trades = TradingRepository.get_trades_by_user(TUI_USER_ID)
            closed = sorted(
                [t for t in all_trades if t.status == "closed"],
                key=lambda x: x.closed_at or datetime.min,
                reverse=True,
            )
            # Only log if count changed
            if len(closed) != self._last_history_count:
                self._log(f"[cyan]History trades count: {self._last_history_count} → {len(closed)}[/cyan]")
                self._last_history_count = len(closed)
            table = self.query_one("#history-table", DataTable)
            table.clear()
            for t in closed:
                pnl = t.profit_loss
                if pnl is not None:
                    pnl_text = (
                        Text(f"+${pnl:.2f}", style="bold green")
                        if pnl >= 0
                        else Text(f"-${abs(pnl):.2f}", style="bold red")
                    )
                else:
                    pnl_text = Text("—")

                type_text = (
                    Text("CALL", style="bold green")
                    if t.trade_type.upper() == "CALL"
                    else Text("PUT", style="bold red")
                )

                exit_str = f"{t.exit_price:.5f}" if t.exit_price else "—"
                closed_str = t.closed_at.strftime("%m-%d %H:%M") if t.closed_at else "—"
                symbol_display = f"{t.symbol} ({get_short_name(t.symbol)})"
                table.add_row(
                    str(t.trade_id), symbol_display, type_text,
                    f"${t.amount:.2f}", f"{t.entry_price:.5f}",
                    exit_str, pnl_text, closed_str,
                )
        except Exception as e:
            self._log(f"[red]History error: {e}[/red]")

    # ─── Command bar ─────────────────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "command-bar":
            return
        raw = event.value.strip()
        event.input.value = ""
        if raw:
            await self._handle_command(raw)

    async def _handle_command(self, raw: str) -> None:
        parts = raw.split()
        cmd = parts[0].lower()

        if cmd == "help":
            self._log(
                "[bold]Commands:[/bold]\n"
                "  place <SYM> <CALL|PUT> <amt> [duration=5] [sl=N] [tp=N]\n"
                "  sell <contract_id>\n"
                "  watch <SYM>  /  unwatch <SYM>\n"
                "  refresh  /  clear  /  help\n"
                "[bold]Keys:[/bold] 1/2/3=tabs  b=buy  s=sell  a=auto-refresh  r=refresh  q=quit"
            )
        elif cmd == "place":
            await self._cmd_place(parts[1:])
        elif cmd == "sell":
            if len(parts) < 2:
                self._log("[yellow]Usage: sell <contract_id>[/yellow]")
            else:
                await self._cmd_sell(parts[1])
        elif cmd == "watch":
            if len(parts) < 2:
                self._log("[yellow]Usage: watch <SYMBOL>[/yellow]")
            else:
                sym = parts[1].upper()
                if sym not in self._watched_symbols:
                    self._watched_symbols.append(sym)
                    self._log(f"Now watching [bold]{sym}[/bold]")
                    # Dynamically mount a new sparkline row
                    sparkline_panel = self.query_one("#sparkline-panel", Vertical)
                    new_row = Horizontal(classes="sparkline-row")
                    sparkline_panel.mount(new_row)
                    new_row.mount(Static(sym, classes="sparkline-label"))
                    new_row.mount(Sparkline(
                        self._price_history.get(sym, [])[-20:],
                        id=f"sparkline-{sym}",
                        classes="sparkline-widget",
                        min_color="#cc4444",
                        max_color="#44cc44",
                    ))
                else:
                    self._log(f"Already watching {sym}")
        elif cmd == "unwatch":
            if len(parts) < 2:
                self._log("[yellow]Usage: unwatch <SYMBOL>[/yellow]")
            else:
                sym = parts[1].upper()
                if sym in self._watched_symbols:
                    self._watched_symbols.remove(sym)
                    self._price_history.pop(sym, None)
                    self._log(f"Stopped watching [bold]{sym}[/bold]")
                    # Remove sparkline row from panel
                    try:
                        sparkline = self.query_one(f"#sparkline-{sym}", Sparkline)
                        sparkline.parent.remove()
                    except Exception:
                        pass
                else:
                    self._log(f"{sym} is not being watched")
        elif cmd == "refresh":
            self.action_refresh_all()
        elif cmd == "clear":
            self.query_one("#activity-log", RichLog).clear()
        else:
            self._log(f"[red]Unknown: '{cmd}'. Type 'help'.[/red]")

    async def _cmd_place(self, args: list[str]) -> None:
        if len(args) < 3:
            self._log(
                "[yellow]Usage: place <SYM> <CALL|PUT> <amount> [duration=5] [sl=N] [tp=N][/yellow]"
            )
            return
        symbol = args[0].upper()
        direction = args[1].upper()
        if direction not in ("CALL", "PUT"):
            self._log("[red]Direction must be CALL or PUT[/red]")
            return
        try:
            amount = float(args[2])
        except ValueError:
            self._log("[red]Amount must be a number[/red]")
            return

        duration, sl, tp = 5, None, None
        for arg in args[3:]:
            if "=" in arg:
                k, v = arg.split("=", 1)
                v = v.strip()  # Strip whitespace
                if not v:  # Skip empty values
                    continue
                try:
                    if k == "duration":
                        duration = int(v)
                    elif k == "sl":
                        sl = float(v)
                    elif k == "tp":
                        tp = float(v)
                except ValueError as e:
                    self._log(f"[red]Invalid {k} value: {v}[/red]")
                    continue

        await self._do_place_trade(symbol, direction, amount, duration, sl, tp)

    async def _do_place_trade(
        self,
        symbol: str,
        direction: str,
        amount: float,
        duration: int = 5,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> None:
        self._log(f"Placing {direction} {symbol} ${amount:.2f} dur={duration}t...")
        try:
            async with self._api_lock:
                if not self._deriv.websocket or self._deriv.websocket.state != State.OPEN:
                    self._deriv.websocket = None
                    await self._deriv.connect()
                resp = await self._deriv.place_contract(
                    symbol=symbol, contract_type=direction,
                    amount=amount, duration=duration, duration_unit="t",
                )
            if "error" in resp:
                error_msg = f"❌ Trade Failed: {resp['error'].get('message')}"
                self._log(f"[red]{error_msg}[/red]")
                self.notify(error_msg, severity="error", timeout=5)
                return

            buy_data = resp.get("buy", {})
            contract_id = str(buy_data.get("contract_id", ""))
            buy_price = float(buy_data.get("buy_price", amount))

            async with self._api_lock:
                hist = await self._deriv.get_ticks_history(symbol, count=2)
            hist_prices = hist.get("history", {}).get("prices", [])
            entry_price = float(hist_prices[-1]) if hist_prices else 0.0

            TradingRepository.create_trade_with_sl_tp(
                user_id=TUI_USER_ID, trade_id=contract_id,
                symbol=symbol, trade_type=direction.lower(),
                amount=buy_price, entry_price=entry_price,
                stop_loss=sl, take_profit=tp,
            )

            # Success notification
            payout = float(buy_data.get("payout", 0))
            success_msg = f"✅ Trade Placed! #{contract_id} - {symbol} {direction} ${buy_price:.2f} (Payout: ${payout:.2f})"
            self._log(f"[bold green]{success_msg}[/bold green]")
            self.notify(success_msg, severity="information", timeout=3)
            self.refresh_open_trades()
        except Exception as e:
            self._log(f"[red]Trade error: {e}[/red]")
            # Also log full traceback for debugging
            import traceback
            self._log(f"[red]Traceback: {traceback.format_exc()}[/red]")

    async def _cmd_sell(self, contract_id: str) -> None:
        self._log(f"Selling #{contract_id}...")
        try:
            async with self._api_lock:
                if not self._deriv.websocket or self._deriv.websocket.state != State.OPEN:
                    self._deriv.websocket = None
                    await self._deriv.connect()
                resp = await self._deriv.sell_contract(contract_id)
            if "error" in resp:
                self._log(f"[red]Sell failed: {resp['error'].get('message')}[/red]")
                return
            sold_for = float(resp.get("sell", {}).get("sold_for", 0))
            self._log(f"[green]#{contract_id} sold for ${sold_for:.2f}[/green]")
            self.refresh_open_trades()
        except Exception as e:
            self._log(f"[red]Sell error: {e}[/red]")

    # ─── Place Trade tab ─────────────────────────────────────────────────────

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        # Handle quick trade buttons
        if event.button.id.startswith("quick-"):
            parts = event.button.id.split("-")  # e.g., "quick-call-r50"
            direction = parts[1].upper()  # CALL or PUT
            symbol = "_".join(parts[2:]).upper()  # R_50, R_100
            await self._do_place_trade(symbol, direction, 1.00, duration=5)
            return

        # Handle custom trade form submission
        if event.button.id != "btn-place":
            return
        symbol = self.query_one("#pt-symbol", Input).value.strip().upper()
        direction = self.query_one("#pt-direction", Input).value.strip().upper()
        amount_str = self.query_one("#pt-amount", Input).value.strip()
        duration_str = self.query_one("#pt-duration", Input).value.strip()
        sl_str = self.query_one("#pt-sl", Input).value.strip()
        tp_str = self.query_one("#pt-tp", Input).value.strip()
        log = self.query_one("#place-log", RichLog)

        if not symbol or not direction or not amount_str:
            log.write("[red]Symbol, Direction, and Amount are required.[/red]")
            return
        if direction not in ("CALL", "PUT"):
            log.write("[red]Direction must be CALL or PUT.[/red]")
            return
        try:
            amount = float(amount_str)
        except ValueError:
            log.write("[red]Amount must be a number.[/red]")
            return

        duration, sl, tp = 5, None, None
        try:
            if duration_str and duration_str.strip():
                duration = int(duration_str.strip())
            if sl_str and sl_str.strip():
                sl = float(sl_str.strip())
            if tp_str and tp_str.strip():
                tp = float(tp_str.strip())
        except ValueError as e:
            self._log(f"[red]Invalid number format: {e}[/red]")
            return

        await self._do_place_trade(symbol, direction, amount, duration, sl, tp)

        # Reset form to defaults after successful trade
        self.query_one("#pt-symbol", Input).value = "R_50"
        self.query_one("#pt-direction", Input).value = "CALL"
        self.query_one("#pt-amount", Input).value = "1.00"
        self.query_one("#pt-duration", Input).value = "5"
        self.query_one("#pt-sl", Input).value = ""
        self.query_one("#pt-tp", Input).value = ""

    # ─── Tab switching ───────────────────────────────────────────────────────

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane = getattr(event, "pane", None)
        if pane and pane.id == "tab-history":
            self.refresh_history()

    # ─── Row selection (trade detail modal) ──────────────────────────────────

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "open-trades-table":
            return
        row_key_val = event.row_key.value if event.row_key.value is not None else str(event.row_key)
        trade = self._open_trade_row_map.get(row_key_val)
        if trade:
            self.push_screen(TradeDetailModal(trade))

    # ─── Actions ─────────────────────────────────────────────────────────────

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab_id

    def action_scroll_down_table(self) -> None:
        focused = self.focused
        if isinstance(focused, DataTable):
            focused.action_scroll_down()

    def action_scroll_up_table(self) -> None:
        focused = self.focused
        if isinstance(focused, DataTable):
            focused.action_scroll_up()

    def action_focus_command_bar(self) -> None:
        self.query_one("#command-bar", Input).focus()

    def action_clear_log(self) -> None:
        try:
            self.query_one("#activity-log", RichLog).clear()
        except Exception:
            pass

    def action_toggle_auto_refresh(self) -> None:
        self._auto_refresh = not self._auto_refresh
        timers = [self._timer_balance, self._timer_open, self._timer_market, self._timer_history]
        for timer in timers:
            if timer is not None:
                if self._auto_refresh:
                    timer.resume()
                else:
                    timer.pause()
        state = "RESUMED" if self._auto_refresh else "PAUSED"
        self._log(f"Auto-refresh [bold]{state}[/bold]")
        self._update_sub_title()

    def action_quick_buy(self) -> None:
        cmd_bar = self.query_one("#command-bar", Input)
        cmd_bar.value = "place "
        cmd_bar.focus()
        cmd_bar.action_end()

    def action_quick_sell(self) -> None:
        cmd_bar = self.query_one("#command-bar", Input)
        cmd_bar.value = "sell "
        cmd_bar.focus()
        cmd_bar.action_end()

    def action_refresh_all(self) -> None:
        self.refresh_balance()
        self.refresh_open_trades()
        self.refresh_market_data()
        self.refresh_history()
        self._log("Refreshing all data...")

    def action_toggle_help(self) -> None:
        self._log(
            "[bold]Keys:[/bold] 1/2/3=tabs  b=quick buy  s=quick sell\n"
            "a=auto-refresh  r=refresh  Ctrl+L=clear log  q=quit  F1=help\n"
            "j/k=scroll table  Esc=focus command bar\n"
            "[bold]Commands:[/bold] place  sell  watch  unwatch  refresh  clear  help"
        )

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _set_connection_status(self, connected: bool) -> None:
        try:
            panel = self.query_one("#status-panel", Static)
            auto = "AUTO" if self._auto_refresh else "MANUAL"
            if connected:
                panel.update(
                    f"[bold green]● LIVE[/bold green]\n"
                    f"Refresh: {auto}"
                )
            else:
                panel.update(
                    f"[bold red]● DISCONNECTED[/bold red]\n"
                    f"Refresh: {auto}"
                )
        except Exception:
            pass

    def _update_sub_title(self) -> None:
        try:
            open_count = len(self._open_trade_row_map)
            auto = "AUTO" if self._auto_refresh else "MANUAL"
            ts = datetime.now().strftime("%H:%M:%S")
            connected_str = "LIVE" if self._auto_refresh else "PAUSED"
            self.sub_title = f"● {connected_str} | {open_count} open | {auto} | {ts}"
        except Exception:
            pass

    def _log(self, message: str) -> None:
        try:
            log = self.query_one("#activity-log", RichLog)
            ts = datetime.now().strftime("%H:%M:%S")
            log.write(f"[dim]{ts}[/dim] {message}")
        except Exception:
            pass


if __name__ == "__main__":
    setup_logging(level="INFO", log_file="trading_tui.log")
    # Redirect trading_bot logger away from stdout so it doesn't corrupt the TUI
    tbot = logging.getLogger("trading_bot")
    tbot.handlers = [
        h for h in tbot.handlers
        if not (isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is sys.stdout)
    ]
    DerivTradingApp().run()
