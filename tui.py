"""Deriv Trading Bot — TUI Dashboard

Connects to the deployed MCP server's REST API.
Set TRADING_API_URL in .env (default: http://trading.enigmata.local).

Run: python tui.py
"""

import asyncio
import logging
import os
import sys
import bisect
from datetime import datetime, timezone
from typing import Optional, Any

import aiohttp
from dotenv import load_dotenv
from rich.text import Text

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
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
    Select,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
)
from textual import work
from textual_plotext import PlotextPlot

from logger import setup_logging
from symbols import get_short_name, get_symbol_display_name

load_dotenv()

API_BASE = os.getenv("TRADING_API_URL", "http://trading.enigmata.local").rstrip("/")
MCP_AGENT_USER_ID = os.getenv("MCP_AGENT_USER_ID", "hermes_agent")

# Live (real-money) MT5 account — Laravel bridge on kreation, reached directly
# over Tailscale (same pattern Hermes uses; the TUI does not go through stinger).
BRIDGE_API_URL = os.getenv("MT5_BRIDGE_URL", "").rstrip("/")
BRIDGE_API_TOKEN = os.getenv("MT5_BRIDGE_TOKEN", "")

DEFAULT_SYMBOLS = ["R_50", "R_75", "R_100", "1HZ50V", "1HZ75V", "1HZ100V"]

ALLOWED_SYMBOLS = sorted([
    # Standard volatility indices
    "R_10", "R_25", "R_50", "R_75", "R_100",
    # 1-second volatility indices (all available)
    "1HZ10V", "1HZ15V", "1HZ25V", "1HZ30V",
    "1HZ50V", "1HZ75V", "1HZ90V", "1HZ100V",
    # Crash/Boom removed 2026-06-17 — blacklisted on the bot (spike strategy failed)
])

# Manual trading was removed from the TUI (2026-06-26, Phase 2): Hermes is the
# sole trader. The quick-trade / multiplier / symbol-multiplier tables that drove
# the old "Trade [2]" tab were deleted along with it. The /api/trade* endpoints
# remain server-side because Hermes uses them.


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

async def api_get(path: str) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_BASE}{path}", timeout=aiohttp.ClientTimeout(total=25)) as r:
            return await r.json()

async def api_post(path: str, body: dict | None = None) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{API_BASE}{path}",
            json=body or {},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            return await r.json()


async def bridge_get(path: str, params: dict | None = None) -> dict[str, Any]:
    """GET against the Laravel MT5 bridge on kreation — the real-money account."""
    headers = {"Authorization": f"Bearer {BRIDGE_API_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{BRIDGE_API_URL}{path}",
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            return await r.json()


# ─── Trade rendering helpers ──────────────────────────────────────────────────
def is_mt5_trade(t: dict[str, Any]) -> bool:
    """MT5 trades carry an mt5_ticket and use lot sizing / price-level SL/TP."""
    return bool(t.get("mt5_ticket"))


def direction_text(t: dict[str, Any]) -> Text:
    """Buy/sell arrow that understands both binary/multiplier and MT5 sides."""
    direction = (t.get("type") or t.get("trade_type") or "").upper()
    is_buy = direction in ("MULTUP", "CALL", "BUY")
    return Text("▲ BUY", style="bold green") if is_buy else Text("▼ SELL", style="bold red")


def size_text(t: dict[str, Any]) -> str:
    """Lot size for MT5 trades (e.g. '0.01'), dollar stake otherwise."""
    amount = t.get("amount", 0) or 0
    return f"{amount:.2f}" if is_mt5_trade(t) else f"${amount:.2f}"


def local_time(iso_str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format an API timestamp (naive UTC ISO) in the viewer's local timezone."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime(fmt)
    except Exception:
        return str(iso_str)[:16].replace("T", " ")


def mt5_epoch_local(epoch, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format an MT5 deal's Unix-epoch `time` field in the viewer's local timezone."""
    if not epoch:
        return "—"
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).astimezone().strftime(fmt)
    except Exception:
        return "—"


# ─── Trade Detail Modal ───────────────────────────────────────────────────────

class TradeDetailModal(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Dismiss"), ("enter", "dismiss", "Dismiss")]

    def __init__(self, trade: dict) -> None:
        super().__init__()
        self._trade = trade

    def compose(self) -> ComposeResult:
        t = self._trade
        pnl = t.get("profit_loss") if t.get("profit_loss") is not None else t.get("unrealized_pnl")
        pnl_str = (f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}") if pnl is not None else "—"
        is_mt5 = is_mt5_trade(t)
        size_label = "Lot Size" if is_mt5 else "Amount"

        rows = [
            ("Trade ID",       str(t.get("trade_id", "—"))),
            ("Symbol",         t.get("symbol", "—")),
            ("Type",           t.get("type", "—")),
            (size_label,       size_text(t)),
            ("Entry Price",    f"{t.get('entry_price', 0):.5f}"),
            ("Current Price",  f"{t.get('current_price', 0):.5f}" if t.get("current_price") else "—"),
            ("P&L",            pnl_str),
            ("Status",         t.get("status", "—").upper()),
            ("Stop Loss",      f"{t['stop_loss']:.5f}"   if t.get("stop_loss")   else "—"),
            ("Take Profit",    f"{t['take_profit']:.5f}" if t.get("take_profit") else "—"),
            ("Opened",         local_time(t.get("created_at"), "%Y-%m-%d %H:%M:%S")),
            ("Closed",         local_time(t.get("closed_at"),  "%Y-%m-%d %H:%M:%S")),
        ]

        mid = len(rows) // 2
        col_a = rows[:mid]
        col_b = rows[mid:]

        with Vertical(id="modal-container"):
            yield Static("── TRADE DETAIL ──", id="modal-title")
            yield Rule()
            with Horizontal(id="modal-grid"):
                with Vertical(id="modal-col-a"):
                    for label, value in col_a:
                        with Horizontal(classes="detail-row"):
                            yield Static(f"{label}:", classes="detail-label")
                            yield Static(value,        classes="detail-value")
                with Vertical(id="modal-col-b"):
                    for label, value in col_b:
                        with Horizontal(classes="detail-row"):
                            yield Static(f"{label}:", classes="detail-label")
                            yield Static(value,        classes="detail-value")
            reason = t.get("reason")
            if reason:
                yield Rule()
                yield Static("Agent reasoning:", classes="detail-label")
                yield Static(str(reason), id="detail-reason")
            yield Rule()
            with Horizontal(id="modal-buttons"):
                yield Button("Dismiss", id="modal-dismiss", variant="default")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "modal-dismiss":
            self.dismiss()


# ─── Main Application ─────────────────────────────────────────────────────────

class DerivTradingApp(App):
    """Deriv Trading Bot — Terminal UI (REST API mode)."""

    CSS = """
    Screen { background: $surface; }

    .panel {
        border: solid $primary;
        padding: 0 1;
        margin: 0 1 1 0;
    }

    /* ── Top bar ───────────────────────────── */
    #top-bar         { height: 7; }
    #account-panel   { width: 36; }
    #portfolio-panel { width: 1fr; }
    #status-panel    { width: 26; }

    /* ── Dashboard split ───────────────────── */
    #main-split { height: 1fr; }
    #left-col   { width: 56%; }
    #right-col  { width: 44%; }

    /* ── DataTable cursor: tint only, keep the red/green cell colors; ──
       blurred (unfocused) shows no highlight, so Esc/Tab acts as deselect. */
    DataTable:focus > .datatable--cursor { background: $primary 30%; }
    DataTable > .datatable--cursor       { background: transparent; }

    #edge-panel { width: 66; padding: 0 1; }

    /* ── Market table ──────────────────────── */
    #market-table { height: 10; }

    /* ── Sparklines ────────────────────────── */
    #sparkline-panel  { height: auto; }
    .sparkline-row    { height: 3; }
    .sparkline-label  { width: 10; color: $text-muted; }
    .sparkline-widget { width: 1fr; height: 3; }

    /* ── Open trades ───────────────────────── */
    #open-trades-table { height: 10; }

    /* ── Signals ───────────────────────────── */
    #signals-table { height: 9; }

    /* ── Pause banner ──────────────────────── */
    #pause-banner {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #pause-banner.pause-active {
        background: $error 30%;
        color: red;
        text-style: bold;
    }
    #pause-banner.pause-clear {
        color: $success;
    }

    /* ── Agent activity ────────────────────── */
    #agent-log { height: 2fr; min-height: 14; }

    /* ── Activity log ──────────────────────── */
    #activity-log { height: 8; min-height: 4; }

    /* ── Trade form ────────────────────────── */
    #agent-tab    { padding: 1 2; height: 1fr; }
    #agent-stream { height: 1fr; margin-top: 1; }
    .section-header {
        color: $accent;
        text-style: bold;
        height: auto;
        margin-bottom: 1;
    }
    Select             { width: 1fr; }

    /* ── History ───────────────────────────── */
    #history-table { height: 1fr; }

    /* ── Live (real-money bridge) ──────────── */
    #live-top-bar        { height: 7; }
    #live-account-panel   { width: 36; }
    #live-portfolio-panel { width: 1fr; }
    #live-status-panel    { width: 26; }
    #live-split          { height: 1fr; }
    #live-positions-table { height: 40%; }
    #live-history-table   { height: 1fr; }

    /* ── Chart ─────────────────────────────── */
    #chart-controls { height: 3; padding: 0 1; align: left middle; }
    #chart-controls Label  { width: auto; padding: 0 1 0 2; content-align: left middle; }
    #chart-controls Select { width: 18; }
    #chart-plot   { height: 1fr; }
    #chart-status { height: 1; padding: 0 1; color: $text-muted; }

    /* ── Modal ─────────────────────────────── */
    #modal-container {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 90;
        height: auto;
        margin: 2 5;
    }
    #modal-title   { text-align: center; color: $accent; text-style: bold; margin-bottom: 1; }
    #modal-grid    { height: auto; }
    #modal-col-a   { width: 1fr; }
    #modal-col-b   { width: 1fr; }
    #modal-buttons { height: 3; margin-top: 1; align: center middle; }
    #modal-buttons Button { margin: 0 1; min-width: 20; }
    .detail-row    { height: 1; }
    .detail-label  { width: 16; color: $text-muted; content-align: right middle; padding-right: 1; }
    .detail-value  { width: 1fr; }
    #detail-reason { width: 1fr; color: $text; padding: 0 1; text-style: italic; }
    """

    BINDINGS = [
        Binding("1", "switch_tab('tab-dashboard')", "Dashboard"),
        Binding("2", "switch_tab('tab-agent')",     "Agent"),
        Binding("3", "switch_tab('tab-history')",   "History"),
        Binding("4", "switch_tab('tab-chart')",     "Chart"),
        Binding("5", "switch_tab('tab-live')",      "Live"),
        Binding("r", "refresh_all",         "Refresh"),
        Binding("a", "toggle_auto_refresh", "Auto"),
        Binding("ctrl+l", "clear_log",      "Clear Log"),
        Binding("escape", "deselect",       "Deselect", show=False),
        Binding("[", "chart_pan(-1)",       "Chart ◀", show=False, priority=True),
        Binding("]", "chart_pan(1)",        "Chart ▶", show=False, priority=True),
        Binding("\\", "chart_pan(0)",       "Chart live", show=False, priority=True),
        Binding("q", "quit",                "Quit"),
        Binding("j", "scroll_down_table",   "↓", show=False),
        Binding("k", "scroll_up_table",     "↑", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._auto_refresh: bool = True
        self._timer_balance: Optional[Timer] = None
        self._timer_open: Optional[Timer] = None
        self._timer_market: Optional[Timer] = None
        self._timer_history: Optional[Timer] = None
        self._timer_agent: Optional[Timer] = None
        self._price_history: dict[str, list[float]] = {}
        self._open_trade_rows: dict[str, dict] = {}
        self._modal_open: bool = False
        self._timer_ticks: Optional[Timer] = None
        self._mkt_col_price: Any = None
        self._mkt_col_change: Any = None
        self._mkt_row_keys: dict[str, Any] = {}   # symbol → RowKey
        self._sig_row_keys: dict[str, Any] = {}   # symbol → RowKey for signals-table
        self._timer_signals: Optional[Timer] = None
        self._chart_symbol: str = "R_100"
        self._chart_tf: str = "1m"
        self._chart_offset: int = 0     # candles scrolled back from the latest (0 = live edge)
        self._timer_chart: Optional[Timer] = None
        self._timer_live_account: Optional[Timer] = None
        self._timer_live_positions: Optional[Timer] = None
        self._timer_live_history: Optional[Timer] = None

    # ─── Layout ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="tabs"):

            # ── Dashboard ──────────────────────────────────────────────────
            with TabPane("Dashboard  [1]", id="tab-dashboard"):
                with Horizontal(id="top-bar"):
                    yield Static("Connecting...", id="account-panel", classes="panel")
                    yield Static("",              id="portfolio-panel", classes="panel")
                    yield Static("",              id="status-panel",   classes="panel")
                yield Static("Pause status: loading…", id="pause-banner")
                with Horizontal(id="main-split"):
                    with Vertical(id="left-col"):
                        yield DataTable(id="market-table", classes="panel", cursor_type="row")
                        with Vertical(id="sparkline-panel", classes="panel"):
                            for sym in DEFAULT_SYMBOLS:
                                with Horizontal(classes="sparkline-row"):
                                    yield Static(get_short_name(sym), classes="sparkline-label")
                                    yield Sparkline(
                                        [],
                                        id=f"sparkline-{sym}",
                                        classes="sparkline-widget",
                                        min_color="#cc4444",
                                        max_color="#44cc44",
                                    )
                    with Vertical(id="right-col"):
                        yield DataTable(id="open-trades-table", classes="panel", cursor_type="row")
                        yield DataTable(id="signals-table", classes="panel", cursor_type="row")
                        yield RichLog(id="agent-log", markup=True, classes="panel", auto_scroll=False)
                yield RichLog(id="activity-log", classes="panel", markup=True)

            # ── Agent (observability; manual trading removed Phase 2) ──────────
            with TabPane("Agent  [2]", id="tab-agent"):
                with Vertical(id="agent-tab"):
                    yield Static("Hermes — waiting for activity…",
                                 id="agent-summary", classes="section-header")
                    yield RichLog(id="agent-stream", markup=True, classes="panel",
                                  auto_scroll=False)

            # ── History ────────────────────────────────────────────────────
            with TabPane("History  [3]", id="tab-history"):
                with Horizontal(id="history-split"):
                    yield DataTable(id="history-table")
                    yield Static("Edge analysis loading…", id="edge-panel",
                                 classes="panel", markup=False)

            # ── Chart ──────────────────────────────────────────────────────
            with TabPane("Chart  [4]", id="tab-chart"):
                with Horizontal(id="chart-controls"):
                    yield Label("Symbol")
                    yield Select(
                        options=[(s, s) for s in ALLOWED_SYMBOLS],
                        id="chart-symbol",
                        value=self._chart_symbol,
                        allow_blank=False,
                    )
                    yield Label("Timeframe")
                    yield Select(
                        options=[(tf, tf) for tf in ("1m", "5m", "15m", "1h")],
                        id="chart-tf",
                        value=self._chart_tf,
                        allow_blank=False,
                    )
                    yield Static("   [dim]◀ [ · ] ▶ · \\ live[/dim]", id="chart-nav-hint")
                yield PlotextPlot(id="chart-plot", classes="panel")
                yield Static("Chart: idle", id="chart-status")

            # ── Live (real-money MT5 account via kreation bridge) ────────────
            with TabPane("Live  [5]", id="tab-live"):
                with Horizontal(id="live-top-bar"):
                    yield Static("Connecting...", id="live-account-panel",   classes="panel")
                    yield Static("",              id="live-portfolio-panel", classes="panel")
                    yield Static("",              id="live-status-panel",   classes="panel")
                with Vertical(id="live-split"):
                    yield DataTable(id="live-positions-table", classes="panel", cursor_type="row")
                    yield DataTable(id="live-history-table", classes="panel", cursor_type="row")

        yield Footer()

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    def on_screen_suspend(self) -> None:
        self._modal_open = True

    def on_screen_resume(self) -> None:
        self._modal_open = False
        self.refresh_open_trades()

    def on_mount(self) -> None:
        mt = self.query_one("#market-table",      DataTable)
        _cols = mt.add_columns("Symbol", "Price", "Change", "RSI", "SMA(14)")
        self._mkt_col_price  = _cols[1]
        self._mkt_col_change = _cols[2]
        mt.border_title = "Market Watch"

        ot = self.query_one("#open-trades-table", DataTable)
        ot.add_columns("ID", "Symbol", "Dir", "Size", "Current", "P&L")
        ot.border_title = "Open Positions"

        st = self.query_one("#signals-table", DataTable)
        st.add_columns("Symbol", "Price", "RSI", "MACD", "BB", "Score", "Call", "Why")
        st.border_title = "Signals  (RSI / MACD / BB → composite)"

        ht = self.query_one("#history-table", DataTable)
        ht.add_columns("ID", "Acct", "Symbol", "Dir", "Size", "Entry", "Exit", "P&L", "Closed")
        ht.border_title = "Trade History"
        self.query_one("#edge-panel", Static).border_title = "Signal Edge"

        lp = self.query_one("#live-positions-table", DataTable)
        lp.add_columns("Ticket", "Symbol", "Dir", "Size", "Open", "Current", "P&L", "SL", "TP")
        self.query_one("#live-account-panel", Static).border_title   = "Account"
        self.query_one("#live-portfolio-panel", Static).border_title = "Portfolio"
        self.query_one("#live-status-panel", Static).border_title    = "Status"
        lp.border_title = "Live — Open Positions"

        lh = self.query_one("#live-history-table", DataTable)
        lh.add_columns("Ticket", "Symbol", "Dir", "Size", "Open", "Close", "P&L", "Closed")
        lh.border_title = "Live — Trade History (7d)"

        chart_plot = self.query_one("#chart-plot", PlotextPlot)
        chart_plot.border_title = "Candlestick Chart"
        # Pin a high-contrast plotext theme: transparent background + default
        # terminal foreground for axis labels (the "auto" theme can render the
        # price axis in a near-invisible colour against the panel surface).
        chart_plot.theme = "pro"

        self.query_one("#sparkline-panel").border_title = "Sparklines  (1m)"
        self.query_one("#agent-log").border_title      = f"Hermes  ({MCP_AGENT_USER_ID})"
        self.query_one("#activity-log").border_title   = "Activity Log"
        self.query_one("#account-panel", Static).border_title  = "Account"
        self.query_one("#portfolio-panel", Static).border_title = "Portfolio"
        self.query_one("#status-panel", Static).border_title   = "Status"

        self._initialize_app()

    @work(exclusive=True, group="init")
    async def _initialize_app(self) -> None:
        self._log(f"Connecting to {API_BASE}...")
        try:
            resp = await api_get("/health")
            if resp.get("status") == "healthy":
                self._log("[green]Server connected[/green]")
                self._set_status(connected=True)
            else:
                self._log("[red]Server unhealthy[/red]")
                self._set_status(connected=False)
                return
        except Exception as e:
            self._log(f"[red]Cannot reach server: {e}[/red]")
            self._set_status(connected=False)
            return

        await self._fetch_balance()
        await self._fetch_open_trades()
        await self._fetch_market_data()
        await self._fetch_agent_activity()
        await self._fetch_agent_stream()
        await self._fetch_agent_summary()
        await self._fetch_history()
        await self._fetch_signals()
        await self._fetch_edge()
        await self._fetch_live_account()
        await self._fetch_live_positions()
        await self._fetch_live_history()

        self._timer_ticks   = self.set_interval(1,  self.refresh_ticks)
        self._timer_balance = self.set_interval(30, self.refresh_balance)
        self._timer_open    = self.set_interval(3,  self.refresh_open_trades)
        self._timer_market  = self.set_interval(5, self.refresh_market_data)
        self._timer_history = self.set_interval(15, self.refresh_history)
        self._timer_edge    = self.set_interval(20, self.refresh_edge)
        self._timer_agent   = self.set_interval(5,  self.refresh_agent_activity)
        self._timer_signals = self.set_interval(3, self.refresh_signals)
        self._timer_chart   = self.set_interval(2, self.refresh_chart)
        self._timer_live_account   = self.set_interval(20, self.refresh_live_account)
        self._timer_live_positions = self.set_interval(5, self.refresh_live_positions)
        self._timer_live_history   = self.set_interval(30, self.refresh_live_history)
        self.refresh_chart()

    # ─── Data fetchers ────────────────────────────────────────────────────────

    async def _fetch_ticks(self) -> None:
        """Pull live prices from the server tick cache, update sparklines and market table."""
        try:
            resp = await api_get("/api/prices")
            if not resp.get("success"):
                return
            prices = resp.get("prices", {})
            if not prices:
                return

            table = self.query_one("#market-table", DataTable)

            for symbol in DEFAULT_SYMBOLS:
                price = prices.get(symbol)
                if price is None:
                    continue
                price = float(price)
                history = self._price_history.get(symbol, [])
                history.append(price)
                self._price_history[symbol] = history[-120:]

                # Update sparkline
                try:
                    self.query_one(f"#sparkline-{symbol}", Sparkline).data = history[-40:]
                except Exception:
                    pass

                # Update Price and Change columns in market table
                rk = self._mkt_row_keys.get(symbol)
                if rk and self._mkt_col_price and self._mkt_col_change and len(history) >= 2:
                    try:
                        change = history[-1] - history[-2]
                        change_text = (
                            Text(f"▲ +{change:.5f}", style="bold green") if change >= 0
                            else Text(f"▼ {change:.5f}", style="bold red")
                        )
                        table.update_cell(rk, self._mkt_col_price,  f"{price:.5f}")
                        table.update_cell(rk, self._mkt_col_change, change_text)
                    except Exception:
                        pass
        except Exception:
            pass

    @work(exclusive=True, group="ticks")
    async def refresh_ticks(self) -> None:
        await self._fetch_ticks()

    async def _fetch_balance(self) -> None:
        try:
            resp = await api_get("/api/balance")
            if resp.get("success"):
                # Response is a string like "Balance: $1234.56 USD"
                data_str = resp.get("data", "")
                self.query_one("#account-panel", Static).update(
                    f"[bold]{data_str}[/bold]"
                )
                self._set_status(connected=True)
        except Exception as e:
            self._log(f"[red]Balance error: {e}[/red]")
            self._set_status(connected=False)

    @work(exclusive=True, group="balance")
    async def refresh_balance(self) -> None:
        await self._fetch_balance()

    async def _fetch_open_trades(self) -> None:
        try:
            resp = await api_get("/api/trades/open")
            if not resp.get("success"):
                return

            trades = resp.get("trades", [])
            total_pnl = resp.get("total_unrealized_pnl", 0)
            # Don't refresh table while modal is open — prevents cursor snapping
            if self._modal_open:
                return
            table = self.query_one("#open-trades-table", DataTable)
            scroll_x = table.scroll_x  # preserve scroll position across the redraw
            scroll_y = table.scroll_y
            table.clear()
            self._open_trade_rows.clear()

            if not trades:
                table.add_row("—", "No open trades", "—", "—", "—", "—", key="empty")
            else:
                for t in trades:
                    pnl = t.get("unrealized_pnl")
                    pnl_text = (
                        Text(f"+${pnl:.2f}", style="bold green") if pnl >= 0
                        else Text(f"-${abs(pnl):.2f}", style="bold red")
                    ) if pnl is not None else Text("—")

                    dir_text = direction_text(t)
                    symbol = t.get("symbol", "")
                    trade_id = str(t.get("trade_id", t.get("id", "")))
                    short_id = f"…{trade_id[-6:]}"
                    current = t.get("current_price", 0)

                    table.add_row(
                        short_id,
                        symbol,
                        dir_text,
                        size_text(t),
                        f"{current:.2f}" if current else "—",
                        pnl_text,
                        key=trade_id,
                    )
                    self._open_trade_rows[trade_id] = t

            # Restore scroll position after redraw
            self.call_after_refresh(lambda: table.scroll_to(scroll_x, scroll_y, animate=False))

            # Update portfolio panel
            stats_resp = await api_get("/api/trades/summary")
            if stats_resp.get("success"):
                s = stats_resp.get("data", {})
                pnl_disp = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
                realized = s.get("total_profit_loss", 0) or 0
                realized_disp = f"+${realized:.2f}" if realized >= 0 else f"-${abs(realized):.2f}"
                self.query_one("#portfolio-panel", Static).update(
                    f"Trades: {s.get('total_trades', 0)}  Win Rate: {s.get('win_rate', 0):.1f}%\n"
                    f"Open: {s.get('open_trades', 0)}  Unrealized P&L: {pnl_disp}\n"
                    f"Closed: {s.get('closed_trades', 0)}  Realized P&L: {realized_disp}"
                )

            self._update_subtitle(len(trades))
        except Exception as e:
            self._log(f"[red]Open trades error: {e}[/red]")

    @work(exclusive=True, group="open_trades")
    async def refresh_open_trades(self) -> None:
        await self._fetch_open_trades()

    async def _fetch_market_data(self) -> None:
        # Collect all data before touching the UI — prevents empty table on cancellation
        rows: list[tuple] = []
        sparklines: dict[str, list[float]] = {}

        for symbol in DEFAULT_SYMBOLS:
            label = f"{symbol} ({get_short_name(symbol)})"
            try:
                # Seed sparkline from candles on first load; subsequent ticks accumulate live
                close_prices: list[float] = []
                if symbol not in self._price_history:
                    candle_resp = await api_get(f"/api/candles/{symbol}?timeframe=1m&count=40")
                    if candle_resp.get("success"):
                        close_prices = [
                            float(c["close"]) for c in candle_resp.get("candles", [])
                            if c.get("close") is not None
                        ]
                    if close_prices:
                        self._price_history[symbol] = close_prices
                else:
                    close_prices = self._price_history.get(symbol, [])
                # Latest tick for current price
                tick_resp = await api_get(f"/api/market-data/{symbol}")
                if not tick_resp.get("success"):
                    rows.append((label, "ERROR", "—", "—", "—"))
                    continue

                data_str = tick_resp.get("data", "")
                parsed = {p.split(":")[0].strip(): p.split(":", 1)[1].strip()
                          for p in data_str.splitlines() if ":" in p}
                price_str = parsed.get("Price", "")
                price = float(price_str) if price_str and price_str != "N/A" else None
                if price is None:
                    rows.append((label, "—", "—", "—", "—"))
                    continue

                # Append latest tick to history and use for sparkline
                prices = self._price_history.get(symbol, [])
                prices.append(price)
                self._price_history[symbol] = prices[-60:]
                sparklines[symbol] = prices[-30:]

                change_text = Text("—")
                if len(prices) >= 2:
                    change = prices[-1] - prices[-2]
                    change_text = (
                        Text(f"▲ +{change:.5f}", style="bold green") if change >= 0
                        else Text(f"▼ {change:.5f}", style="bold red")
                    )

                # Calculate indicators from accumulated price history (includes live ticks)
                rsi_text = Text("—")
                sma_str = "—"
                # Use the running tick history — grows with every 1-second tick update
                all_prices = self._price_history.get(symbol, [])
                indicator_src = all_prices if len(all_prices) >= 15 else (
                    close_prices if len(close_prices) >= 15 else None
                )
                if indicator_src:
                    from indicators import TechnicalIndicators
                    rsi_list = TechnicalIndicators.calculate_rsi(indicator_src)
                    sma_list = TechnicalIndicators.calculate_sma(indicator_src, 14)
                    if rsi_list and rsi_list[-1] is not None:
                        rsi_val = rsi_list[-1]
                        style = "bold red" if rsi_val > 70 else ("bold green" if rsi_val < 30 else "yellow")
                        rsi_text = Text(f"{rsi_val:.1f}", style=style)
                    if sma_list and sma_list[-1] is not None:
                        sma_str = f"{sma_list[-1]:.5f}"

                rows.append((label, f"{price:.5f}", change_text, rsi_text, sma_str))

            except Exception as e:
                rows.append((label, "ERROR", "—", "—", "—"))
                self._log(f"[red]Market error {symbol}: {e}[/red]")

        # Atomic UI update — only runs if we have data
        if not rows:
            return
        try:
            table = self.query_one("#market-table", DataTable)
            table.clear()
            self._mkt_row_keys.clear()
            for symbol, row in zip(DEFAULT_SYMBOLS, rows):
                rk = table.add_row(*row, key=symbol)
                self._mkt_row_keys[symbol] = rk
            for symbol, data in sparklines.items():
                try:
                    self.query_one(f"#sparkline-{symbol}", Sparkline).data = data
                except Exception:
                    pass
        except Exception as e:
            self._log(f"[red]Market table update error: {e}[/red]")

    @work(exclusive=True, group="market_data")
    async def refresh_market_data(self) -> None:
        await self._fetch_market_data()

    async def _fetch_history(self) -> None:
        try:
            resp = await api_get("/api/trades/list")
            if not resp.get("success"):
                return
            trades = [t for t in resp.get("trades", []) if t.get("status") == "closed"]
            trades.sort(key=lambda t: t.get("closed_at") or "", reverse=True)
            table = self.query_one("#history-table", DataTable)
            scroll_x, scroll_y = table.scroll_x, table.scroll_y  # preserve across redraw
            table.clear()
            for t in trades:
                pnl = t.get("profit_loss")
                pnl_text = (
                    Text(f"+${pnl:.2f}", style="bold green") if pnl >= 0
                    else Text(f"-${abs(pnl):.2f}", style="bold red")
                ) if pnl is not None else Text("—")
                dir_text = direction_text(t)
                symbol = t.get("symbol", "")
                trade_id = str(t.get("trade_id", t.get("id", "")))
                closed_short = local_time(t.get("closed_at"), "%Y-%m-%d %H:%M")

                exit_price = t.get("exit_price") or 0
                exit_str = f"{exit_price:.5f}" if exit_price else "—"

                entry = t.get("entry_price") or 0
                entry_str = f"{entry:.5f}" if entry else "—"

                is_live = t.get("account_type") == "live"
                acct_text = Text("LIVE", style="bold red") if is_live else Text("demo", style="dim")

                table.add_row(
                    f"…{trade_id[-6:]}",
                    acct_text,
                    symbol,
                    dir_text,
                    size_text(t),
                    entry_str,
                    exit_str,
                    pnl_text,
                    closed_short,
                )
            self.call_after_refresh(lambda: table.scroll_to(scroll_x, scroll_y, animate=False))
        except Exception as e:
            self._log(f"[red]History error: {e}[/red]")

    @work(exclusive=True, group="history")
    async def refresh_history(self) -> None:
        await self._fetch_history()

    async def _fetch_live_account(self) -> None:
        acct_panel = self.query_one("#live-account-panel", Static)
        port_panel = self.query_one("#live-portfolio-panel", Static)
        stat_panel = self.query_one("#live-status-panel", Static)

        if not BRIDGE_API_URL or not BRIDGE_API_TOKEN:
            acct_panel.update("[yellow]MT5_BRIDGE_URL /\nMT5_BRIDGE_TOKEN\nnot set in .env[/yellow]")
            port_panel.update("")
            stat_panel.update("[bold red]● OFFLINE[/bold red]\nnot configured")
            return
        try:
            data = await bridge_get("/account")
            if not isinstance(data, dict) or data.get("error"):
                err = data.get("error", "unknown") if isinstance(data, dict) else "bad response"
                acct_panel.update(f"[red]{err}[/red]")
                port_panel.update("")
                stat_panel.update(f"[bold red]● OFFLINE[/bold red]\n{BRIDGE_API_URL}")
                return

            balance = data.get("balance") or 0
            equity = data.get("equity") or 0
            margin = data.get("margin") or 0
            margin_free = data.get("margin_free") or 0
            profit = data.get("profit") or 0
            login = data.get("login", "—")
            server = data.get("server", "—")
            currency = data.get("currency", "")
            leverage = data.get("leverage", "—")
            trade_allowed = data.get("trade_allowed", False)

            acct_panel.update(
                f"[bold]{login}[/bold]\n{server}\n"
                f"Balance: [bold]${balance:.2f}[/bold] {currency}"
            )
            pnl_style = "bold green" if profit >= 0 else "bold red"
            port_panel.update(
                f"Equity: ${equity:.2f}   Margin: ${margin:.2f}   Free: ${margin_free:.2f}\n"
                f"[{pnl_style}]Open P&L: ${profit:.2f}[/{pnl_style}]"
            )
            stat_panel.update(
                f"[bold red]● LIVE[/bold red]\n"
                f"Leverage 1:{leverage}\n"
                f"{'Trading enabled' if trade_allowed else '[red]Trading disabled[/red]'}"
            )
        except Exception as e:
            acct_panel.update(f"[red]Live account error: {e}[/red]")
            port_panel.update("")
            stat_panel.update("[bold red]● OFFLINE[/bold red]")

    @work(exclusive=True, group="live_account")
    async def refresh_live_account(self) -> None:
        await self._fetch_live_account()

    async def _fetch_live_positions(self) -> None:
        if not BRIDGE_API_URL or not BRIDGE_API_TOKEN:
            return
        try:
            positions = await bridge_get("/positions")
            if not isinstance(positions, list):
                return
            table = self.query_one("#live-positions-table", DataTable)
            scroll_x, scroll_y = table.scroll_x, table.scroll_y
            table.clear()
            if not positions:
                table.add_row("—", "No open positions", "—", "—", "—", "—", "—", "—", "—", key="empty")
            else:
                for p in positions:
                    pnl = p.get("profit") or 0
                    pnl_text = (Text(f"+${pnl:.2f}", style="bold green") if pnl >= 0
                                else Text(f"-${abs(pnl):.2f}", style="bold red"))
                    dir_text = (Text("▲ BUY", style="bold green") if p.get("type") == 0
                                else Text("▼ SELL", style="bold red"))
                    ticket = str(p.get("ticket", ""))
                    sl, tp = p.get("sl") or 0, p.get("tp") or 0
                    table.add_row(
                        f"…{ticket[-6:]}" if len(ticket) > 6 else ticket,
                        p.get("symbol", ""),
                        dir_text,
                        f"{p.get('volume', 0):.2f}",
                        f"{p.get('price_open', 0):.5f}",
                        f"{p.get('price_current', 0):.5f}",
                        pnl_text,
                        f"{sl:.5f}" if sl else "—",
                        f"{tp:.5f}" if tp else "—",
                        key=ticket,
                    )
            self.call_after_refresh(lambda: table.scroll_to(scroll_x, scroll_y, animate=False))
        except Exception as e:
            self._log(f"[red]Live positions error: {e}[/red]")

    @work(exclusive=True, group="live_positions")
    async def refresh_live_positions(self) -> None:
        await self._fetch_live_positions()

    async def _fetch_live_history(self) -> None:
        if not BRIDGE_API_URL or not BRIDGE_API_TOKEN:
            return
        try:
            deals = await bridge_get("/history", params={"days": 7})
            if not isinstance(deals, list):
                return
            # The bridge returns raw MT5 deals (one IN + one OUT leg per closed
            # position, linked by position_id) — pair them into one row per trade.
            by_position: dict[Any, dict] = {}
            for d in deals:
                pos_id = d.get("position_id")
                if pos_id is None:
                    continue
                row = by_position.setdefault(pos_id, {})
                if d.get("entry") == 0:  # DEAL_ENTRY_IN
                    row.update(
                        open_price=d.get("price"), open_time=d.get("time"),
                        dir="BUY" if d.get("type") == 0 else "SELL",
                        volume=d.get("volume"), symbol=d.get("symbol"), ticket=pos_id,
                    )
                elif d.get("entry") == 1:  # DEAL_ENTRY_OUT
                    row["close_price"] = d.get("price")
                    row["close_time"] = d.get("time")
                    row["profit"] = (row.get("profit") or 0) + (d.get("profit") or 0)

            rows = [r for r in by_position.values() if "close_time" in r]
            rows.sort(key=lambda r: r.get("close_time") or 0, reverse=True)

            table = self.query_one("#live-history-table", DataTable)
            scroll_x, scroll_y = table.scroll_x, table.scroll_y
            table.clear()
            for r in rows:
                pnl = r.get("profit") or 0
                pnl_text = (Text(f"+${pnl:.2f}", style="bold green") if pnl >= 0
                            else Text(f"-${abs(pnl):.2f}", style="bold red"))
                dir_text = (Text("▲ BUY", style="bold green") if r.get("dir") == "BUY"
                            else Text("▼ SELL", style="bold red"))
                ticket = str(r.get("ticket", ""))
                table.add_row(
                    f"…{ticket[-6:]}" if len(ticket) > 6 else ticket,
                    r.get("symbol", ""),
                    dir_text,
                    f"{r.get('volume', 0):.2f}",
                    f"{r.get('open_price', 0):.5f}",
                    f"{r.get('close_price', 0):.5f}",
                    pnl_text,
                    mt5_epoch_local(r.get("close_time")),
                )
            self.call_after_refresh(lambda: table.scroll_to(scroll_x, scroll_y, animate=False))
        except Exception as e:
            self._log(f"[red]Live history error: {e}[/red]")

    @work(exclusive=True, group="live_history")
    async def refresh_live_history(self) -> None:
        await self._fetch_live_history()

    async def _fetch_edge(self) -> None:
        try:
            resp = await api_get("/api/analysis/edge")
            panel = self.query_one("#edge-panel", Static)
            panel.update(resp.get("report", "no data") if resp.get("success")
                         else "Edge analysis unavailable")
        except Exception as e:
            try:
                self.query_one("#edge-panel", Static).update(f"Edge error: {e}")
            except Exception:
                pass

    @work(exclusive=True, group="edge")
    async def refresh_edge(self) -> None:
        await self._fetch_edge()

    async def _fetch_agent_activity(self) -> None:
        try:
            resp = await api_get("/api/trades/agent")
            log = self.query_one("#agent-log", RichLog)
            # Preserve the reader's scroll position across the rebuild: only
            # snap to the newest entry if they were already at the bottom.
            prev_y = log.scroll_y
            at_bottom = (log.max_scroll_y - log.scroll_y) <= 1
            log.clear()
            if not resp.get("success"):
                log.write("[dim]Agent endpoint unavailable[/dim]")
                return
            trades = resp.get("trades", [])
            if not trades:
                log.write(f"[dim]Waiting for {MCP_AGENT_USER_ID}...[/dim]")
                log.write(f"[dim]MCP endpoint: {API_BASE}/mcp[/dim]")
                return
            recent = sorted(trades, key=lambda t: t.get("created_at") or "", reverse=True)[:30]
            for t in recent:
                ts = local_time(t.get("created_at"), "%H:%M")
                direction = t.get("type", t.get("trade_type", "")).upper()
                is_buy = direction in ("MULTUP", "CALL", "BUY")
                dir_color = "green" if is_buy else "red"
                dir_arrow = "▲" if is_buy else "▼"
                pnl = t.get("profit_loss")
                status = t.get("status", "")
                if status == "open":
                    result = "[cyan]● open[/cyan]"
                elif pnl is not None and pnl > 0:
                    result = f"[bold green]+${pnl:.2f}[/bold green]"
                elif pnl is not None:
                    result = f"[bold red]-${abs(pnl):.2f}[/bold red]"
                else:
                    result = "[dim]—[/dim]"
                symbol = t.get("symbol", "")
                log.write(
                    f"[dim]{ts}[/dim] "
                    f"[{dir_color}]{dir_arrow} {direction}[/{dir_color}] "
                    f"[bold]{symbol}[/bold] "
                    f"{size_text(t)} → {result}"
                )
                reason = t.get("reason")
                if reason:
                    log.write(f"   [dim italic]↳ {str(reason)[:110]}[/dim italic]")
            self.call_after_refresh(
                lambda: log.scroll_end(animate=False) if at_bottom
                else log.scroll_to(y=prev_y, animate=False)
            )
        except Exception as e:
            self._log(f"[yellow]Activity feed error: {e}[/yellow]")

    async def _fetch_agent_stream(self) -> None:
        """Render the full per-scan reasoning stream (Phase 4) into the Agent tab —
        every scan including the silent ones, so the agent's decisions are visible
        even when it places no trade."""
        try:
            resp = await api_get("/api/agent/activity?limit=100")
            log = self.query_one("#agent-stream", RichLog)
            prev_y = log.scroll_y
            at_bottom = (log.max_scroll_y - log.scroll_y) <= 1
            log.clear()
            if not resp.get("success"):
                log.write("[dim]Activity endpoint unavailable[/dim]")
                return
            items = resp.get("activity", [])
            if not items:
                log.write("[dim]No agent activity recorded yet.[/dim]")
                log.write("[dim]Hermes scans every ~10 min; per-scan records land here.[/dim]")
                return
            # API returns newest-first; render oldest-first so the newest is at the bottom.
            for a in reversed(items):
                ts = local_time(a.get("ts"), "%H:%M:%S")
                etype = (a.get("event_type") or "scan").lower()
                sym = a.get("symbol") or ""
                score = a.get("score")
                decision = (a.get("decision") or "").upper()
                detail = a.get("detail") or ""
                if decision in ("BUY", "CALL", "MULTUP", "LONG"):
                    dcol = "bold green"
                elif decision in ("SELL", "PUT", "MULTDOWN", "SHORT"):
                    dcol = "bold red"
                elif etype == "error":
                    dcol = "bold yellow"
                else:
                    dcol = "dim"
                icon = {"trade": "◆", "signal": "▸", "error": "✕",
                        "heartbeat": "♥"}.get(etype, "·")
                line = f"[dim]{ts}[/dim] {icon}"
                if sym:
                    line += f" [bold]{sym}[/bold]"
                if isinstance(score, (int, float)):
                    line += f" [cyan]{score:+.1f}[/cyan]"
                if decision:
                    line += f" [{dcol}]{decision}[/{dcol}]"
                log.write(line)
                if detail:
                    log.write(f"   [dim italic]↳ {str(detail)[:140]}[/dim italic]")
            self.call_after_refresh(
                lambda: log.scroll_end(animate=False) if at_bottom
                else log.scroll_to(y=prev_y, animate=False)
            )
        except Exception as e:
            self._log(f"[yellow]Agent stream error: {e}[/yellow]")

    async def _fetch_agent_summary(self) -> None:
        """Win/loss summary strip atop the Agent tab, reusing /api/portfolio/stats."""
        try:
            resp = await api_get("/api/portfolio/stats")
            if not resp.get("success"):
                return
            s = resp.get("stats", {})
            w = s.get("winning_trades", 0)
            l = s.get("losing_trades", 0)
            wr = s.get("win_rate", 0)
            net = s.get("total_pnl", 0) or 0
            openn = s.get("open_trades", 0)
            ncol = "green" if net >= 0 else "red"
            self.query_one("#agent-summary", Static).update(
                f"[bold]Hermes[/bold]   "
                f"W [green]{w}[/green] / L [red]{l}[/red]   "
                f"win-rate [bold]{wr}%[/bold]   "
                f"net [{ncol}]{net:+.2f}[/{ncol}]   "
                f"open [cyan]{openn}[/cyan]"
            )
        except Exception:
            pass

    @work(exclusive=True, group="agent_activity")
    async def refresh_agent_activity(self) -> None:
        await self._fetch_agent_activity()
        await self._fetch_agent_stream()
        await self._fetch_agent_summary()

    # ─── Chart ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_iso(s: str) -> Optional[datetime]:
        """Parse an API ISO timestamp (naive UTC) into a *local* naive datetime,
        so trade markers align with the locally-rendered candle times."""
        try:
            dt = datetime.fromisoformat(s.replace("Z", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone().replace(tzinfo=None)
        except Exception:
            return None

    async def _fetch_chart(self) -> None:
        symbol = self._chart_symbol
        tf = self._chart_tf
        status = self.query_one("#chart-status", Static)
        plot = self.query_one("#chart-plot", PlotextPlot)
        try:
            # Fetch a deep history so the user can pan back through it.
            resp = await api_get(f"/api/candles/{symbol}?timeframe={tf}&count=500")
            if not resp.get("success"):
                status.update(f"[red]Chart: candles unavailable for {symbol}[/red]")
                return
            candles = [c for c in resp.get("candles", []) if c.get("close") is not None]
            candles.sort(key=lambda c: c.get("time") or 0)
            if not candles:
                status.update(f"[yellow]Chart: no candle data for {symbol}[/yellow]")
                return

            # Cap visible candles to what fits the plot width with even spacing.
            # Plotting more candles than available columns makes plotext squash
            # some together (they look paired); ~2 columns per candle keeps gaps
            # even. The y-axis labels eat ~8 columns.
            width = plot.size.width or 120
            max_candles = max(20, (width - 8) // 2)
            # Window = a max_candles-wide slice positioned by the pan offset
            # (0 = live edge). Clamp the offset to the available history.
            total = len(candles)
            self._chart_offset = max(0, min(self._chart_offset, max(0, total - max_candles)))
            end = total - self._chart_offset
            start = max(0, end - max_candles)
            candles = candles[start:end]

            times = [datetime.fromtimestamp(int(c["time"]))  # local naive (matches _parse_iso)
                     for c in candles]
            data = {
                "Open":  [float(c["open"])  for c in candles],
                "High":  [float(c["high"])  for c in candles],
                "Low":   [float(c["low"])   for c in candles],
                "Close": [float(c["close"]) for c in candles],
            }

            # MetaTrader-style overlay: ▲/▼ entry arrows at the entry price
            # (green=long, red=short), ● exit markers at the exit price (green
            # win / red loss), a cyan entry-price line for open positions, and an
            # orange live price line. plt.text places glyphs reliably at data
            # coordinates (scatter markers don't render dependably).
            t0, t1 = times[0], times[-1]
            last = data["Close"][-1]
            n = len(times)

            def idx_of(ts):
                """Nearest candle index for a timestamp — markers sit on the
                evenly-spaced candle slots (not a datetime axis)."""
                j = bisect.bisect_left(times, ts)
                if j <= 0:
                    return 0
                if j >= n:
                    return n - 1
                return j if (times[j] - ts) <= (ts - times[j - 1]) else j - 1

            entries: list[tuple] = []   # (x_index, price, glyph, color)
            exits: list[tuple] = []     # (x_index, price, color)
            deals: list[tuple] = []     # (x0, y0, x1, y1, color) entry→exit connector
            pnl_labels: list[tuple] = []# (x_index, price, text, color) value at exit
            sltp_lines: list[tuple] = []# (price, color) SL/TP guides for open positions
            open_lines: list[float] = []
            n_open = n_closed = off_screen = sym_trades = 0
            recent_summary: Optional[str] = None
            # small vertical nudge so P&L labels don't sit exactly on the ● glyph
            y_pad = (max(data["High"]) - min(data["Low"])) * 0.03
            try:
                tr = await api_get("/api/trades/agent")
                if tr.get("success"):
                    chart_name = get_symbol_display_name(symbol)  # R_75 -> "Volatility 75 Index"
                    for t in sorted(tr.get("trades", []), key=lambda x: x.get("created_at") or ""):
                        if t.get("symbol") not in (symbol, chart_name):
                            continue
                        sym_trades += 1
                        ca = self._parse_iso(t.get("created_at") or "")
                        ep = t.get("entry_price")
                        if not (ca and ep and t0 <= ca <= t1):
                            off_screen += 1
                            continue
                        ep = float(ep)
                        ex0 = idx_of(ca)
                        is_up = str(t.get("type", "")).lower() in ("call", "buy", "multup")
                        sign = 1 if is_up else -1
                        entries.append((ex0, ep, "▲" if is_up else "▼", "green" if is_up else "red"))
                        if t.get("status") == "open":
                            n_open += 1
                            open_lines.append(ep)
                            # SL/TP guide lines for the live position (price levels)
                            sl, tp = t.get("stop_loss"), t.get("take_profit")
                            if sl:
                                sltp_lines.append((float(sl), "red"))
                            if tp:
                                sltp_lines.append((float(tp), "green"))
                            recent_summary = f"{'▲' if is_up else '▼'} open {(last - ep) * sign:+.2f} pts (floating)"
                        else:
                            n_closed += 1
                            xp = t.get("exit_price"); cl = self._parse_iso(t.get("closed_at") or "")
                            pnl = t.get("profit_loss")
                            if xp:
                                xp = float(xp)
                                win_color = "green" if (pnl or 0) >= 0 else "red"
                                if cl and t0 <= cl <= t1:
                                    ex1 = idx_of(cl)
                                    exits.append((ex1, xp, win_color))
                                    # MT5 "deal" line: entry → exit, colored by outcome
                                    deals.append((ex0, ep, ex1, xp, win_color))
                                    if pnl is not None:
                                        ly = xp + (y_pad if pnl >= 0 else -y_pad)
                                        pnl_labels.append((ex1, ly, f"{pnl:+.2f}", win_color))
                                pnl_s = f"${pnl:+.2f}" if pnl is not None else "?"
                                recent_summary = f"{'▲' if is_up else '▼'} {(xp - ep) * sign:+.2f} pts → {pnl_s}"
            except Exception:
                pass

            plt = plot.plt
            plt.clear_figure()
            # Plot candles at even integer slots (not a datetime axis) so spacing
            # is uniform on every timeframe — plotext's datetime mode rounds to
            # columns and leaves gaps ("scatter"), worst on sparse 1h data.
            xs = list(range(n))
            plt.candlestick(xs, data)
            plt.horizontal_line(last, color="orange")           # live current price
            for price, color in sltp_lines:                      # SL (red) / TP (green) for open positions
                plt.horizontal_line(price, color=color)
            for y in open_lines:                                 # open-position entry price
                plt.horizontal_line(y, color="cyan")
            for x0, y0, x1, y1, color in deals:                  # entry→exit "deal" line, win/loss colored
                plt.plot([x0, x1], [y0, y1], color=color)
            for x, price, glyph, color in entries:               # entry arrows at entry price
                plt.text(glyph, x, price, color=color)
            for x, price, color in exits:                        # exit markers at exit price
                plt.text("●", x, price, color=color)
            for x, price, text, color in pnl_labels:             # P&L value at each closed exit
                plt.text(text, x, price, color=color)
            # Time labels at ~6 evenly-spaced candle slots.
            if n:
                step = max(1, n // 6)
                ticks = list(range(0, n, step))
                plt.xticks(ticks, [times[i].strftime("%H:%M") for i in ticks])
            plt.title(f"{symbol} · {tf}")
            plot.refresh()

            parts = [f"{symbol} {tf}", f"last {last:.5f}"]
            if self._chart_offset > 0:
                parts.append(f"◀ history −{self._chart_offset} ( ] forward · \\ live )")
            if n_open or n_closed:
                parts.append(f"▲▼ {n_open} open / {n_closed} closed ●")
            if recent_summary:
                parts.append(f"latest: {recent_summary}")
            if off_screen:
                parts.append(f"{off_screen} off-screen")
            if sym_trades == 0:
                parts.append("no agent trades on this symbol")
            status.update(" · ".join(parts))
        except Exception as e:
            status.update(f"[red]Chart error: {e}[/red]")

    @work(exclusive=True, group="chart")
    async def refresh_chart(self) -> None:
        await self._fetch_chart()

    async def _fetch_signals(self) -> None:
        """Pull pause-status and all signals from the batch endpoint; update banner + table."""
        symbols_qs = ",".join(DEFAULT_SYMBOLS)
        try:
            resp = await api_get(f"/api/signals?symbols={symbols_qs}")
        except Exception as e:
            self._log(f"[yellow]signals fetch error: {e}[/yellow]")
            return

        if not resp.get("success"):
            self._log(f"[yellow]signals fetch failed: {resp.get('error')}[/yellow]")
            return

        p = resp.get("pause", {}) or {}
        banner = self.query_one("#pause-banner", Static)
        banner.remove_class("pause-active")
        banner.remove_class("pause-clear")
        if p.get("recommend_pause"):
            banner.add_class("pause-active")
            banner.update(
                f"⚠  PAUSE RECOMMENDED — {p.get('current_streak', 0)} consecutive losses "
                f"(threshold {p.get('threshold', 0)}, max ever {p.get('max_streak', 0)})"
            )
        else:
            banner.add_class("pause-clear")
            banner.update(
                f"✓ Streak OK — current {p.get('current_streak', 0)} / "
                f"threshold {p.get('threshold', 0)} / max {p.get('max_streak', 0)}"
            )

        signals_map = resp.get("signals", {}) or {}
        for sym, err in (resp.get("errors") or {}).items():
            self._log(f"[yellow]signal {sym} error: {err}[/yellow]")

        rows: list[tuple] = [
            (sym, signals_map[sym]) for sym in DEFAULT_SYMBOLS if sym in signals_map
        ]
        if not rows:
            return

        try:
            table = self.query_one("#signals-table", DataTable)
            ts = datetime.now().strftime("%H:%M:%S")
            table.border_title = f"Signals  (RSI / MACD / BB → composite)  · updated {ts}"
            table.clear()
            self._sig_row_keys.clear()
            for sym, sig in rows:
                price = sig.get("current_price")
                rsi = sig.get("rsi") or {}
                macd = sig.get("macd") or {}
                bb = sig.get("bb") or {}
                call = sig.get("call", "HOLD")
                score = sig.get("composite_score", 0)
                why = sig.get("reason", "")

                price_text = f"{price:.5f}" if isinstance(price, (int, float)) else "—"

                # RSI: oversold (<30) = bullish bias = green; overbought (>70) = bearish = red
                rsi_v = rsi.get("value")
                rsi_score = rsi.get("score", 0)
                if isinstance(rsi_v, (int, float)):
                    rsi_style = (
                        "bold green" if rsi_score == 1
                        else "bold red" if rsi_score == -1
                        else "dim"
                    )
                    rsi_text = Text(f"{rsi_v:.1f} {rsi.get('label', '')}".strip(),
                                    style=rsi_style)
                else:
                    rsi_text = Text("—", style="dim")

                # MACD-hist: positive = bullish = green; negative = bearish = red
                macd_h = macd.get("hist")
                macd_score = macd.get("score", 0)
                if isinstance(macd_h, (int, float)):
                    macd_style = (
                        "bold green" if macd_score == 1
                        else "bold red" if macd_score == -1
                        else "dim"
                    )
                    macd_text = Text(f"{macd_h:+.4f} {macd.get('label', '')}".strip(),
                                     style=macd_style)
                else:
                    macd_text = Text("—", style="dim")

                # BB position: below-lower = mean-revert buy = green;
                # above-upper = mean-revert sell = red; within = grey
                bb_pos = bb.get("position", "—")
                bb_score = bb.get("score", 0)
                bb_style = (
                    "bold green" if bb_score == 1
                    else "bold red" if bb_score == -1
                    else "dim"
                )
                bb_text = Text(bb_pos, style=bb_style)

                # Composite score: + = bullish lean, - = bearish lean
                if score >= 2:
                    score_style = "bold green"
                elif score == 1:
                    score_style = "green"
                elif score <= -2:
                    score_style = "bold red"
                elif score == -1:
                    score_style = "red"
                else:
                    score_style = "dim"
                score_text = Text(f"{score:+d}", style=score_style)

                if call == "BUY":
                    call_text = Text("▲ BUY", style="bold green")
                elif call == "SELL":
                    call_text = Text("▼ SELL", style="bold red")
                else:
                    call_text = Text("HOLD", style="dim")

                rk = table.add_row(
                    sym, price_text, rsi_text, macd_text, bb_text,
                    score_text, call_text, why,
                    key=sym,
                )
                self._sig_row_keys[sym] = rk
        except Exception as e:
            self._log(f"[red]Signals table update error: {e}[/red]")

    @work(exclusive=True, group="signals")
    async def refresh_signals(self) -> None:
        await self._fetch_signals()

    # ─── Event handlers ───────────────────────────────────────────────────────

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.tab.id == "tab-history":
            self.refresh_history()
        elif event.tab.id == "tab-chart":
            self.refresh_chart()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "chart-symbol" and event.value is not Select.BLANK:
            self._chart_symbol = str(event.value)
            self._chart_offset = 0
            self.refresh_chart()
        elif event.select.id == "chart-tf" and event.value is not Select.BLANK:
            self._chart_tf = str(event.value)
            self._chart_offset = 0
            self.refresh_chart()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Row keys in open trades table are trade_id strings; other tables use symbol names
        # or auto-generated keys — so this lookup is safe without an ID check
        trade = self._open_trade_rows.get(str(event.row_key.value))
        if trade:
            self.push_screen(TradeDetailModal(trade))

    # ─── Actions ─────────────────────────────────────────────────────────────

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab_id

    def action_scroll_down_table(self) -> None:
        try:
            self.query_one("#open-trades-table", DataTable).action_scroll_down()
        except Exception:
            pass

    def action_scroll_up_table(self) -> None:
        try:
            self.query_one("#open-trades-table", DataTable).action_scroll_up()
        except Exception:

            pass

    def action_clear_log(self) -> None:
        try:
            self.query_one("#activity-log", RichLog).clear()
        except Exception:
            pass

    def action_deselect(self) -> None:
        """Drop focus from the current table so its row highlight clears
        (the blurred cursor is transparent)."""
        try:
            self.screen.set_focus(None)
        except Exception:
            pass

    def action_chart_pan(self, direction: int) -> None:
        """Pan the chart through history: [ = older, ] = newer, \\ = back to live.
        Pans by half a screen so context is kept. direction 0 resets to the edge."""
        if direction == 0:
            self._chart_offset = 0
        else:
            try:
                width = self.query_one("#chart-plot", PlotextPlot).size.width or 120
            except Exception:
                width = 120
            step = max(5, (max(20, (width - 8) // 2)) // 2)
            # [ (direction -1) pans back into history (offset up); ] pans forward.
            self._chart_offset = max(0, self._chart_offset - direction * step)
        self.refresh_chart()

    def action_toggle_auto_refresh(self) -> None:
        self._auto_refresh = not self._auto_refresh
        timers = [self._timer_ticks, self._timer_balance, self._timer_open, self._timer_market,
                  self._timer_history, self._timer_agent, self._timer_chart,
                  self._timer_live_account, self._timer_live_positions, self._timer_live_history]
        if self._auto_refresh:
            self._timer_ticks   = self.set_interval(1,  self.refresh_ticks)
            self._timer_balance = self.set_interval(30, self.refresh_balance)
            self._timer_open    = self.set_interval(3,  self.refresh_open_trades)
            self._timer_market  = self.set_interval(5, self.refresh_market_data)
            self._timer_history = self.set_interval(15, self.refresh_history)
            self._timer_edge    = self.set_interval(20, self.refresh_edge)
            self._timer_agent   = self.set_interval(5,  self.refresh_agent_activity)
            self._timer_chart   = self.set_interval(2, self.refresh_chart)
            self._timer_live_account   = self.set_interval(20, self.refresh_live_account)
            self._timer_live_positions = self.set_interval(5, self.refresh_live_positions)
            self._timer_live_history   = self.set_interval(30, self.refresh_live_history)
            self._log("[green]Auto-refresh ON[/green]")
        else:
            for t in timers:
                if t:
                    t.stop()
            self._log("[yellow]Auto-refresh OFF[/yellow]")
        self._set_status(connected=self._auto_refresh)

    def action_refresh_all(self) -> None:
        self.refresh_balance()
        self.refresh_open_trades()
        self.refresh_market_data()
        self.refresh_history()
        self.refresh_agent_activity()

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _set_status(self, connected: bool) -> None:
        try:
            auto = "AUTO" if self._auto_refresh else "MANUAL"
            status = "[bold green]● LIVE[/bold green]" if connected else "[bold red]● OFFLINE[/bold red]"
            self.query_one("#status-panel", Static).update(f"{status}\n{API_BASE}\nRefresh: {auto}")
        except Exception:
            pass

    def _update_subtitle(self, open_count: int = 0) -> None:
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            self.sub_title = f"{'AUTO' if self._auto_refresh else 'PAUSED'} | {open_count} open | {ts}"
        except Exception:
            pass

    def _log(self, message: str) -> None:
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            self.query_one("#activity-log", RichLog).write(f"[dim]{ts}[/dim] {message}")
        except Exception:
            pass


if __name__ == "__main__":
    setup_logging(level="INFO", log_file="trading_tui.log")
    tbot = logging.getLogger("trading_bot")
    tbot.handlers = [
        h for h in tbot.handlers
        if not (isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is sys.stdout)
    ]
    DerivTradingApp().run()
