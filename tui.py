"""Deriv Trading Bot — TUI Dashboard

Connects to the deployed MCP server's REST API.
Set TRADING_API_URL in .env (default: http://trading.enigmata.local).

Run: python tui.py
"""

import asyncio
import logging
import os
import sys
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
from symbols import get_short_name

load_dotenv()

API_BASE = os.getenv("TRADING_API_URL", "http://trading.enigmata.local").rstrip("/")
MCP_AGENT_USER_ID = os.getenv("MCP_AGENT_USER_ID", "hermes_agent")

DEFAULT_SYMBOLS = ["R_50", "R_75", "R_100", "1HZ50V", "1HZ75V", "1HZ100V"]

ALLOWED_SYMBOLS = sorted([
    # Standard volatility indices
    "R_10", "R_25", "R_50", "R_75", "R_100",
    # 1-second volatility indices (all available)
    "1HZ10V", "1HZ15V", "1HZ25V", "1HZ30V",
    "1HZ50V", "1HZ75V", "1HZ90V", "1HZ100V",
])

QUICK_TRADES: dict[str, tuple[str, str, float]] = {
    "quick-call-r50":     ("CALL", "R_50",    1.00),
    "quick-put-r50":      ("PUT",  "R_50",    1.00),
    "quick-call-r100":    ("CALL", "R_100",   1.00),
    "quick-put-r100":     ("PUT",  "R_100",   1.00),
    "quick-call-1hz50v":  ("CALL", "1HZ50V",  1.00),
    "quick-put-1hz50v":   ("PUT",  "1HZ50V",  1.00),
    "quick-call-1hz100v": ("CALL", "1HZ100V", 1.00),
    "quick-put-1hz100v":  ("PUT",  "1HZ100V", 1.00),
}

# (direction, symbol, amount, multiplier)
QUICK_MULTIPLIERS: dict[str, tuple[str, str, float, int]] = {
    "qm-buy-r50":     ("BUY",  "R_50",    1.00, 80),
    "qm-sell-r50":    ("SELL", "R_50",    1.00, 80),
    "qm-buy-r100":    ("BUY",  "R_100",   1.00, 40),
    "qm-sell-r100":   ("SELL", "R_100",   1.00, 40),
    "qm-buy-1hz50v":  ("BUY",  "1HZ50V",  1.00, 80),
    "qm-sell-1hz50v": ("SELL", "1HZ50V",  1.00, 80),
}

SYMBOL_MULTIPLIERS: dict[str, list[int]] = {
    # Standard volatility
    "R_10":  [100, 200, 500],
    "R_25":  [50, 100, 200],
    "R_50":  [80, 200, 400, 600, 800],
    "R_75":  [20, 50, 100],
    "R_100": [40, 100, 200, 300, 400],
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


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

async def api_get(path: str) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_BASE}{path}", timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.json()

async def api_post(path: str, body: dict | None = None) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{API_BASE}{path}",
            json=body or {},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            return await r.json()


# ─── Trade Detail Modal ───────────────────────────────────────────────────────

class TradeDetailModal(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Dismiss"), ("enter", "dismiss", "Dismiss")]

    def __init__(self, trade: dict, on_close=None) -> None:
        super().__init__()
        self._trade = trade
        self._on_close = on_close  # callback(trade_id) for closing a multiplier position

    def compose(self) -> ComposeResult:
        t = self._trade
        pnl = t.get("profit_loss") if t.get("profit_loss") is not None else t.get("unrealized_pnl")
        pnl_str = (f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}") if pnl is not None else "—"
        is_open = t.get("status", "").lower() == "open"
        is_multiplier = t.get("type", "").upper() in ("MULTUP", "MULTDOWN")

        rows = [
            ("Trade ID",       str(t.get("trade_id", "—"))),
            ("Symbol",         t.get("symbol", "—")),
            ("Type",           t.get("type", "—")),
            ("Amount",         f"${t.get('amount', 0):.2f}"),
            ("Entry Price",    f"{t.get('entry_price', 0):.5f}"),
            ("Current Price",  f"{t.get('current_price', 0):.5f}" if t.get("current_price") else "—"),
            ("P&L",            pnl_str),
            ("Status",         t.get("status", "—").upper()),
            ("Stop Loss",      f"{t['stop_loss']:.5f}"   if t.get("stop_loss")   else "—"),
            ("Take Profit",    f"{t['take_profit']:.5f}" if t.get("take_profit") else "—"),
            ("Opened",         (t.get("created_at", "—") or "—")[:19].replace("T", " ")),
            ("Closed",         (t.get("closed_at",  "—") or "—")[:19].replace("T", " ")),
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
            yield Rule()
            with Horizontal(id="modal-buttons"):
                yield Button("✕  Close Position", id="modal-close-trade", variant="error",
                             disabled=not is_open)
                yield Button("Dismiss", id="modal-dismiss", variant="default")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "modal-dismiss":
            self.dismiss()
        elif event.button.id == "modal-close-trade":
            trade_id = str(self._trade.get("trade_id", ""))
            callback = self._on_close
            self.dismiss()
            if callback:
                await callback(trade_id)


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

    /* ── Market table ──────────────────────── */
    #market-table { height: 10; }

    /* ── Sparklines ────────────────────────── */
    #sparkline-panel  { height: auto; }
    .sparkline-row    { height: 3; }
    .sparkline-label  { width: 10; color: $text-muted; }
    .sparkline-widget { width: 1fr; height: 3; }

    /* ── Open trades ───────────────────────── */
    #open-trades-table { height: 12; }

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
    #agent-log { height: 1fr; min-height: 5; }

    /* ── Activity log ──────────────────────── */
    #activity-log { height: 8; min-height: 4; }

    /* ── Trade form ────────────────────────── */
    #trade-form { padding: 1 2; overflow-y: auto; height: 1fr; }
    .section-header {
        color: $accent;
        text-style: bold;
        height: 1;
        margin-bottom: 1;
    }
    .quick-grid        { height: auto; }
    .quick-grid Button { margin: 0 1 1 0; min-width: 20; }
    .form-row          { height: 3; margin-bottom: 1; align: left middle; }
    .form-label        { width: 14; content-align: right middle; padding-right: 2; }
    Select             { width: 1fr; }
    .dir-btn           { width: 14; margin-right: 1; }
    .mode-btn          { width: 18; margin-right: 1; }
    .mult-btn          { width: 8; margin-right: 1; }
    .amt-btn           { width: 7; margin-right: 1; }
    #mult-row          { height: 3; margin-bottom: 1; align: left middle; }
    #duration-row      { height: 3; margin-bottom: 1; align: left middle; }
    #btn-place         { margin-top: 1; min-width: 28; }
    #place-log         { height: 8; margin-top: 1; }

    /* ── History ───────────────────────────── */
    #history-table { height: 1fr; }

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
    """

    BINDINGS = [
        Binding("1", "switch_tab('tab-dashboard')", "Dashboard"),
        Binding("2", "switch_tab('tab-place')",     "Trade"),
        Binding("3", "switch_tab('tab-history')",   "History"),
        Binding("4", "switch_tab('tab-chart')",     "Chart"),
        Binding("c", "close_trade",         "Close Trade"),
        Binding("r", "refresh_all",         "Refresh"),
        Binding("a", "toggle_auto_refresh", "Auto"),
        Binding("ctrl+l", "clear_log",      "Clear Log"),
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
        self._trade_mode: str = "multiplier"  # "binary" or "multiplier"
        self._trade_direction: str = "BUY"    # BUY/SELL for multiplier, CALL/PUT for binary
        self._multiplier: int = 80
        self._modal_open: bool = False
        self._timer_ticks: Optional[Timer] = None
        self._mkt_col_price: Any = None
        self._mkt_col_change: Any = None
        self._mkt_row_keys: dict[str, Any] = {}   # symbol → RowKey
        self._sig_row_keys: dict[str, Any] = {}   # symbol → RowKey for signals-table
        self._timer_signals: Optional[Timer] = None
        self._chart_symbol: str = "R_100"
        self._chart_tf: str = "1m"
        self._timer_chart: Optional[Timer] = None

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
                        yield RichLog(id="agent-log", markup=True, classes="panel")
                yield RichLog(id="activity-log", classes="panel", markup=True)

            # ── Place Trade ────────────────────────────────────────────────
            with TabPane("Trade  [2]", id="tab-place"):
                with Vertical(id="trade-form"):
                    yield Static("▸ Mode", classes="section-header")
                    with Horizontal(classes="form-row"):
                        yield Label("Type", classes="form-label")
                        yield Button("✦ Multiplier", id="mode-multiplier", variant="primary",   classes="mode-btn")
                        yield Button("  Binary Opt", id="mode-binary",     variant="default",   classes="mode-btn")
                    yield Rule()
                    yield Static("▸ Quick Trade", classes="section-header")
                    with Horizontal(id="quick-mult-grid", classes="quick-grid"):
                        yield Button("▲ BUY  R_50  $1 80x",  id="qm-buy-r50",     variant="success")
                        yield Button("▼ SELL R_50  $1 80x",  id="qm-sell-r50",    variant="error")
                        yield Button("▲ BUY  R_100 $1 40x",  id="qm-buy-r100",    variant="success")
                        yield Button("▼ SELL R_100 $1 40x",  id="qm-sell-r100",   variant="error")
                        yield Button("▲ BUY  1HZ50V $1 80x", id="qm-buy-1hz50v",  variant="success")
                        yield Button("▼ SELL 1HZ50V $1 80x", id="qm-sell-1hz50v", variant="error")
                    with Horizontal(id="quick-bin-grid", classes="quick-grid"):
                        yield Button("▲ CALL  R_50  $1",  id="quick-call-r50",     variant="success")
                        yield Button("▼ PUT   R_50  $1",  id="quick-put-r50",      variant="error")
                        yield Button("▲ CALL  R_100 $1",  id="quick-call-r100",    variant="success")
                        yield Button("▼ PUT   R_100 $1",  id="quick-put-r100",     variant="error")
                        yield Button("▲ CALL 1HZ100V $1", id="quick-call-1hz100v", variant="success")
                        yield Button("▼ PUT  1HZ100V $1", id="quick-put-1hz100v",  variant="error")
                    yield Rule()
                    yield Static("▸ Custom Trade", classes="section-header")
                    with Horizontal(classes="form-row"):
                        yield Label("Symbol", classes="form-label")
                        yield Select(
                            options=[(s, s) for s in ALLOWED_SYMBOLS],
                            id="pt-symbol",
                            value="R_50",
                            allow_blank=False,
                        )
                    with Horizontal(classes="form-row"):
                        yield Label("Direction", classes="form-label")
                        yield Button("▲  BUY / CALL",  id="dir-call", variant="success", classes="dir-btn")
                        yield Button("▼  SELL / PUT",  id="dir-put",  variant="default", classes="dir-btn")
                    with Horizontal(classes="form-row"):
                        yield Label("Amount ($)", classes="form-label")
                        yield Input(placeholder="1.00", id="pt-amount", value="1.00", type="number")
                    with Horizontal(classes="form-row"):
                        yield Label("Presets", classes="form-label")
                        yield Button("$0.35", id="amt-035", variant="default", classes="amt-btn")
                        yield Button("$1",    id="amt-1",   variant="default", classes="amt-btn")
                        yield Button("$2",    id="amt-2",   variant="default", classes="amt-btn")
                        yield Button("$5",    id="amt-5",   variant="default", classes="amt-btn")
                        yield Button("$10",   id="amt-10",  variant="default", classes="amt-btn")
                        yield Button("$25",   id="amt-25",  variant="default", classes="amt-btn")
                        yield Button("$50",   id="amt-50",  variant="default", classes="amt-btn")
                    yield Static("", id="min-stake-note", classes="section-header")
                    with Horizontal(id="mult-row", classes="form-row"):
                        yield Label("Multiplier", classes="form-label")
                        yield Button("80x",  id="mult-80",  variant="primary",  classes="mult-btn")
                        yield Button("200x", id="mult-200", variant="default",  classes="mult-btn")
                        yield Button("400x", id="mult-400", variant="default",  classes="mult-btn")
                        yield Button("600x", id="mult-600", variant="default",  classes="mult-btn")
                        yield Button("800x", id="mult-800", variant="default",  classes="mult-btn")
                    with Horizontal(id="duration-row", classes="form-row"):
                        yield Label("Duration (s)", classes="form-label")
                        yield Input(placeholder="60", id="pt-duration", value="60", type="integer")
                    with Horizontal(classes="form-row"):
                        yield Label("Stop Loss ($)", classes="form-label")
                        yield Input(placeholder="optional loss amount", id="pt-sl")
                    with Horizontal(classes="form-row"):
                        yield Label("Take Profit ($)", classes="form-label")
                        yield Input(placeholder="optional profit amount", id="pt-tp")
                    yield Button("▶  Place Trade", id="btn-place", variant="primary")
                    yield RichLog(id="place-log", markup=True, classes="panel")

            # ── History ────────────────────────────────────────────────────
            with TabPane("History  [3]", id="tab-history"):
                yield DataTable(id="history-table")

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
                yield PlotextPlot(id="chart-plot", classes="panel")
                yield Static("Chart: idle", id="chart-status")

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
        ot.add_columns("ID", "Symbol", "Dir", "Stake", "Current", "P&L")
        ot.border_title = "Open Positions"

        st = self.query_one("#signals-table", DataTable)
        st.add_columns("Symbol", "Price", "RSI", "MACD", "BB", "Score", "Call", "Why")
        st.border_title = "Signals  (RSI / MACD / BB → composite)"

        ht = self.query_one("#history-table", DataTable)
        ht.add_columns("ID", "Symbol", "Dir", "Stake", "Entry", "Exit", "P&L", "Closed")
        ht.border_title = "Trade History"

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

        self.call_after_refresh(self._apply_mode)
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
        await self._fetch_history()
        await self._fetch_signals()

        self._timer_ticks   = self.set_interval(1,  self.refresh_ticks)
        self._timer_balance = self.set_interval(30, self.refresh_balance)
        self._timer_open    = self.set_interval(3,  self.refresh_open_trades)
        self._timer_market  = self.set_interval(5, self.refresh_market_data)
        self._timer_history = self.set_interval(15, self.refresh_history)
        self._timer_agent   = self.set_interval(5,  self.refresh_agent_activity)
        self._timer_signals = self.set_interval(3, self.refresh_signals)
        self._timer_chart   = self.set_interval(2, self.refresh_chart)
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
            scroll_x = table.scroll_x  # preserve horizontal scroll position
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

                    direction = t.get("type", "").upper()
                    dir_text = (
                        Text("▲ BUY",  style="bold green") if direction in ("MULTUP",  "CALL")
                        else Text("▼ SELL", style="bold red")
                    )
                    symbol = t.get("symbol", "")
                    trade_id = str(t.get("trade_id", t.get("id", "")))
                    short_id = f"…{trade_id[-6:]}"
                    current = t.get("current_price", 0)

                    table.add_row(
                        short_id,
                        symbol,
                        dir_text,
                        f"${t.get('amount', 0):.2f}",
                        f"{current:.2f}" if current else "—",
                        pnl_text,
                        key=trade_id,
                    )
                    self._open_trade_rows[trade_id] = t

            # Restore scroll position after redraw
            self.call_after_refresh(lambda: table.scroll_to(scroll_x, 0, animate=False))

            # Update portfolio panel
            stats_resp = await api_get("/api/trades/summary")
            if stats_resp.get("success"):
                s = stats_resp.get("data", {})
                sign = "+" if total_pnl >= 0 else ""
                self.query_one("#portfolio-panel", Static).update(
                    f"Trades: {s.get('total_trades', 0)}  Win Rate: {s.get('win_rate', 0):.1f}%\n"
                    f"Open: {s.get('open_trades', 0)}  Unrealized P&L: {sign}${total_pnl:.2f}"
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
                    from deriv_client import TechnicalIndicators
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
            table.clear()
            for t in trades:
                pnl = t.get("profit_loss")
                pnl_text = (
                    Text(f"+${pnl:.2f}", style="bold green") if pnl >= 0
                    else Text(f"-${abs(pnl):.2f}", style="bold red")
                ) if pnl is not None else Text("—")
                direction = t.get("type", t.get("trade_type", "")).upper()
                dir_text = (
                    Text("▲ BUY",  style="bold green") if direction in ("MULTUP", "CALL")
                    else Text("▼ SELL", style="bold red")
                )
                symbol = t.get("symbol", "")
                trade_id = str(t.get("trade_id", t.get("id", "")))
                closed = t.get("closed_at", "")
                closed_short = closed[:16].replace("T", " ") if closed else "—"

                exit_price = t.get("exit_price") or 0
                exit_str = f"{exit_price:.5f}" if exit_price else "—"

                entry = t.get("entry_price") or 0
                entry_str = f"{entry:.5f}" if entry else "—"

                table.add_row(
                    f"…{trade_id[-6:]}",
                    symbol,
                    dir_text,
                    f"${t.get('amount', 0):.2f}",
                    entry_str,
                    exit_str,
                    pnl_text,
                    closed_short,
                )
        except Exception as e:
            self._log(f"[red]History error: {e}[/red]")

    @work(exclusive=True, group="history")
    async def refresh_history(self) -> None:
        await self._fetch_history()

    async def _fetch_agent_activity(self) -> None:
        try:
            resp = await api_get("/api/trades/agent")
            log = self.query_one("#agent-log", RichLog)
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
                ts_raw = t.get("created_at", "")
                ts = ts_raw[11:16] if len(ts_raw) >= 16 else "--:--"
                direction = t.get("type", t.get("trade_type", "")).upper()
                dir_color = "green" if direction == "CALL" else "red"
                dir_arrow = "▲" if direction == "CALL" else "▼"
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
                    f"${t.get('amount', 0):.2f} → {result}"
                )
        except Exception as e:
            self._log(f"[yellow]Activity feed error: {e}[/yellow]")

    @work(exclusive=True, group="agent_activity")
    async def refresh_agent_activity(self) -> None:
        await self._fetch_agent_activity()

    # ─── Chart ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_iso(s: str) -> Optional[datetime]:
        """Parse an API ISO timestamp into a naive-UTC datetime (to match candle epochs)."""
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None

    async def _fetch_chart(self) -> None:
        symbol = self._chart_symbol
        tf = self._chart_tf
        status = self.query_one("#chart-status", Static)
        plot = self.query_one("#chart-plot", PlotextPlot)
        try:
            resp = await api_get(f"/api/candles/{symbol}?timeframe={tf}&count=120")
            if not resp.get("success"):
                status.update(f"[red]Chart: candles unavailable for {symbol}[/red]")
                return
            candles = [c for c in resp.get("candles", []) if c.get("close") is not None]
            candles.sort(key=lambda c: c.get("time") or 0)
            if not candles:
                status.update(f"[yellow]Chart: no candle data for {symbol}[/yellow]")
                return

            # Cap candle count to what fits the plot width with even spacing.
            # Plotting more candles than available columns makes plotext squash
            # some together (they look paired); ~2 columns per candle keeps gaps
            # even. The y-axis labels eat ~8 columns.
            width = plot.size.width or 120
            max_candles = max(20, (width - 8) // 2)
            if len(candles) > max_candles:
                candles = candles[-max_candles:]

            times = [datetime.fromtimestamp(int(c["time"]), timezone.utc).replace(tzinfo=None)
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
            entries: list[tuple] = []   # (datetime, price, glyph, color)
            exits: list[tuple] = []     # (datetime, price, color)
            open_lines: list[float] = []
            n_open = n_closed = off_screen = sym_trades = 0
            recent_summary: Optional[str] = None
            try:
                tr = await api_get("/api/trades/agent")
                if tr.get("success"):
                    for t in sorted(tr.get("trades", []), key=lambda x: x.get("created_at") or ""):
                        if t.get("symbol") != symbol:
                            continue
                        sym_trades += 1
                        ca = self._parse_iso(t.get("created_at") or "")
                        ep = t.get("entry_price")
                        if not (ca and ep and t0 <= ca <= t1):
                            off_screen += 1
                            continue
                        ep = float(ep)
                        is_up = str(t.get("type", "")).lower() in ("call", "buy", "multup")
                        sign = 1 if is_up else -1
                        entries.append((ca, ep, "▲" if is_up else "▼", "green" if is_up else "red"))
                        if t.get("status") == "open":
                            n_open += 1
                            open_lines.append(ep)
                            recent_summary = f"{'▲' if is_up else '▼'} open {(last - ep) * sign:+.2f} pts (floating)"
                        else:
                            n_closed += 1
                            xp = t.get("exit_price"); cl = self._parse_iso(t.get("closed_at") or "")
                            pnl = t.get("profit_loss")
                            if xp:
                                xp = float(xp)
                                if cl and t0 <= cl <= t1:
                                    exits.append((cl, xp, "green" if (pnl or 0) >= 0 else "red"))
                                pnl_s = f"${pnl:+.2f}" if pnl is not None else "?"
                                recent_summary = f"{'▲' if is_up else '▼'} {(xp - ep) * sign:+.2f} pts → {pnl_s}"
            except Exception:
                pass

            plt = plot.plt
            plt.clear_figure()
            plt.date_form("H:M")
            plt.candlestick([plt.datetime_to_string(t) for t in times], data)
            plt.horizontal_line(last, color="orange")           # live current price
            for y in open_lines:                                 # open-position entry price
                plt.horizontal_line(y, color="cyan")
            for ca, price, glyph, color in entries:              # entry arrows at entry price
                plt.text(glyph, plt.datetime_to_string(ca), price, color=color)
            for cl, price, color in exits:                       # exit markers at exit price
                plt.text("●", plt.datetime_to_string(cl), price, color=color)
            plt.title(f"{symbol} · {tf}")
            plot.refresh()

            parts = [f"{symbol} {tf}", f"last {last:.5f}"]
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

    async def _close_trade(self, trade_id: str) -> None:
        """Close an open multiplier position."""
        self._log(f"Closing #{trade_id}...")
        try:
            resp = await api_post(f"/api/trade/{trade_id}/sell", {})
            if resp.get("success"):
                pnl = resp.get("data", {}).get("profit_loss", 0)
                sign = "+" if float(pnl) >= 0 else ""
                self._log(f"[green]#{trade_id} closed: {sign}${float(pnl):.2f}[/green]")
                self.notify(f"Closed #{trade_id}: {sign}${float(pnl):.2f}", severity="information", timeout=3)
            else:
                self._log(f"[red]Close failed: {resp.get('error')}[/red]")
                self.notify(f"Close failed: {resp.get('error')}", severity="error", timeout=5)
            self.refresh_open_trades()
            self.refresh_history()
            self.refresh_balance()
        except Exception as e:
            self._log(f"[red]Close error: {e}[/red]")

    # ─── Trade placement ──────────────────────────────────────────────────────

    async def _do_place_multiplier(
        self,
        symbol: str,
        direction: str,
        amount: float,
        multiplier: int,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> None:
        self._log(f"Placing {direction} {symbol} ${amount:.2f} @ {multiplier}x...")
        try:
            body: dict = {
                "symbol": symbol,
                "direction": direction,
                "amount": amount,
                "multiplier": multiplier,
            }
            if sl is not None:
                body["stop_loss"] = sl
            if tp is not None:
                body["take_profit"] = tp
            resp = await api_post("/api/trade/multiplier", body)
            if resp.get("success"):
                msg = f"✅ {resp.get('data', 'Multiplier trade placed')}"
                self._log(f"[bold green]{msg}[/bold green]")
                self.notify(msg, severity="information", timeout=4)
                self.refresh_open_trades()
                self.set_timer(1.5, self.refresh_open_trades)
                self.set_timer(4.0, self.refresh_open_trades)
                self.refresh_balance()
            else:
                msg = f"❌ {resp.get('error', 'Trade failed')}"
                self._log(f"[red]{msg}[/red]")
                self.notify(msg, severity="error", timeout=5)
        except Exception as e:
            self._log(f"[red]Multiplier trade error: {e}[/red]")

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
            body: dict = {
                "symbol": symbol,
                "direction": direction,
                "amount": amount,
                "duration": duration,
            }
            if sl is not None:
                body["stop_loss"] = sl
            if tp is not None:
                body["take_profit"] = tp

            resp = await api_post("/api/trade", body)
            if resp.get("success"):
                msg = f"✅ {resp.get('data', 'Trade placed')}"
                self._log(f"[bold green]{msg}[/bold green]")
                self.notify(msg, severity="information", timeout=3)
                self.refresh_open_trades()
                self.set_timer(1.5, self.refresh_open_trades)
                self.set_timer(4.0, self.refresh_open_trades)
                self.refresh_balance()
            else:
                msg = f"❌ {resp.get('error', 'Trade failed')}"
                self._log(f"[red]{msg}[/red]")
                self.notify(msg, severity="error", timeout=5)
        except Exception as e:
            self._log(f"[red]Trade error: {e}[/red]")

    # ─── Event handlers ───────────────────────────────────────────────────────

    def _apply_mode(self) -> None:
        """Show/hide widgets based on current trade mode."""
        try:
            mult = self._trade_mode == "multiplier"
            self.query_one("#mode-multiplier", Button).variant = "primary" if mult else "default"
            self.query_one("#mode-binary",     Button).variant = "default" if mult else "primary"
            self.query_one("#quick-mult-grid").display = mult
            self.query_one("#quick-bin-grid").display  = not mult
            self.query_one("#mult-row").display         = mult
            self.query_one("#duration-row").display     = not mult
            self.query_one("#amt-035").display          = not mult
            self.query_one("#min-stake-note", Static).update(
                "Min stake: $1.00" if mult else "Min stake: $0.35"
            )
            # Update direction button labels
            call_btn = self.query_one("#dir-call", Button)
            put_btn  = self.query_one("#dir-put",  Button)
            call_btn.label = "▲  BUY"  if mult else "▲  CALL"
            put_btn.label  = "▼  SELL" if mult else "▼  PUT"
        except Exception:
            pass

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id

        # Trade mode toggle
        if bid == "mode-multiplier":
            self._trade_mode = "multiplier"
            self._trade_direction = "BUY"
            self.query_one("#dir-call", Button).variant = "success"
            self.query_one("#dir-put",  Button).variant = "default"
            self._apply_mode()
            return
        if bid == "mode-binary":
            self._trade_mode = "binary"
            self._trade_direction = "CALL"
            self.query_one("#dir-call", Button).variant = "success"
            self.query_one("#dir-put",  Button).variant = "default"
            self._apply_mode()
            return

        # Direction toggle
        if bid == "dir-call":
            self._trade_direction = "BUY" if self._trade_mode == "multiplier" else "CALL"
            self.query_one("#dir-call", Button).variant = "success"
            self.query_one("#dir-put",  Button).variant = "default"
            return
        if bid == "dir-put":
            self._trade_direction = "SELL" if self._trade_mode == "multiplier" else "PUT"
            self.query_one("#dir-put",  Button).variant = "error"
            self.query_one("#dir-call", Button).variant = "default"
            return

        # Amount presets
        amt_map = {"amt-035": "0.35", "amt-1": "1.00", "amt-2": "2.00",
                   "amt-5": "5.00", "amt-10": "10.00", "amt-25": "25.00", "amt-50": "50.00"}
        if bid in amt_map:
            self.query_one("#pt-amount", Input).value = amt_map[bid]
            return

        # Multiplier selector
        if bid.startswith("mult-"):
            try:
                self._multiplier = int(bid.split("-")[1])
                for m in [80, 200, 400, 600, 800]:
                    try:
                        self.query_one(f"#mult-{m}", Button).variant = "primary" if m == self._multiplier else "default"
                    except Exception:
                        pass
            except (ValueError, IndexError):
                pass
            return

        # Quick multiplier buttons
        if bid in QUICK_MULTIPLIERS:
            direction, symbol, amount, mult = QUICK_MULTIPLIERS[bid]
            await self._do_place_multiplier(symbol, direction, amount, mult)
            return

        # Quick binary buttons
        if bid in QUICK_TRADES:
            direction, symbol, amount = QUICK_TRADES[bid]
            await self._do_place_trade(symbol, direction, amount, duration=60)
            return

        if bid == "btn-place":
            symbol_val = self.query_one("#pt-symbol", Select).value
            symbol = str(symbol_val) if symbol_val is not Select.BLANK else ""
            log = self.query_one("#place-log", RichLog)
            if not symbol:
                log.write("[red]Please select a symbol.[/red]")
                return

            amount_str = self.query_one("#pt-amount", Input).value.strip()
            sl_str     = self.query_one("#pt-sl",     Input).value.strip()
            tp_str     = self.query_one("#pt-tp",     Input).value.strip()
            try:
                amount = float(amount_str or "1.00")
            except ValueError:
                log.write("[red]Amount must be a number.[/red]")
                return
            sl = float(sl_str) if sl_str else None
            tp = float(tp_str) if tp_str else None

            if self._trade_mode == "multiplier":
                await self._do_place_multiplier(symbol, self._trade_direction, amount, self._multiplier, sl, tp)
            else:
                duration_str = self.query_one("#pt-duration", Input).value.strip()
                duration = int(duration_str) if duration_str else 60
                await self._do_place_trade(symbol, self._trade_direction, amount, duration, sl, tp)

            self.query_one("#pt-sl", Input).value = ""
            self.query_one("#pt-tp", Input).value = ""

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.tab.id == "tab-history":
            self.refresh_history()
        elif event.tab.id == "tab-chart":
            self.refresh_chart()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "chart-symbol" and event.value is not Select.BLANK:
            self._chart_symbol = str(event.value)
            self.refresh_chart()
        elif event.select.id == "chart-tf" and event.value is not Select.BLANK:
            self._chart_tf = str(event.value)
            self.refresh_chart()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Row keys in open trades table are trade_id strings; other tables use symbol names
        # or auto-generated keys — so this lookup is safe without an ID check
        trade = self._open_trade_rows.get(str(event.row_key.value))
        if trade:
            self.push_screen(TradeDetailModal(trade, on_close=self._close_trade))

    # ─── Actions ─────────────────────────────────────────────────────────────

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab_id

    async def action_close_trade(self) -> None:
        """Close the currently highlighted row in the Open Positions table."""
        try:
            table = self.query_one("#open-trades-table", DataTable)
            row_keys = list(table.rows.keys())
            if not row_keys or table.cursor_row >= len(row_keys):
                self._log("[yellow]No trade selected to close.[/yellow]")
                return
            rk = row_keys[table.cursor_row]
            trade = self._open_trade_rows.get(str(rk.value))
            if not trade:
                self._log("[yellow]Select a row in Open Positions then press C.[/yellow]")
                return
            trade_id = str(trade.get("trade_id", ""))
            await self._close_trade(trade_id)
        except Exception as e:
            self._log(f"[red]Close error: {e}[/red]")

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

    def action_toggle_auto_refresh(self) -> None:
        self._auto_refresh = not self._auto_refresh
        timers = [self._timer_ticks, self._timer_balance, self._timer_open, self._timer_market,
                  self._timer_history, self._timer_agent, self._timer_chart]
        if self._auto_refresh:
            self._timer_ticks   = self.set_interval(1,  self.refresh_ticks)
            self._timer_balance = self.set_interval(30, self.refresh_balance)
            self._timer_open    = self.set_interval(3,  self.refresh_open_trades)
            self._timer_market  = self.set_interval(5, self.refresh_market_data)
            self._timer_history = self.set_interval(15, self.refresh_history)
            self._timer_agent   = self.set_interval(5,  self.refresh_agent_activity)
            self._timer_chart   = self.set_interval(2, self.refresh_chart)
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
