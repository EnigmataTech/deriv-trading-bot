# Deriv Trading Bot

A terminal-based binary options trading bot for the [Deriv](https://deriv.com) platform. Ships two runtimes in one codebase:

- **TUI** (`tui.py`) — full-screen Textual dashboard for interactive trading
- **MCP API Server** (`main.py`) — FastMCP server exposing trading tools over HTTP for n8n / AI agent integration

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   tui.py  (TUI)                     │
│  Dashboard · History · Place Trade · Command Bar    │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│              deriv_client.py                        │
│  WebSocket ↔ wss://ws.binaryws.com/websockets/v3   │
│  get_ticks_history · place_contract · sell_contract │
│  TechnicalIndicators: SMA · RSI                     │
└────────────────────┬────────────────────────────────┘
                     │
       ┌─────────────┴─────────────┐
       │                           │
┌──────▼──────┐          ┌─────────▼──────────┐
│ database.py │          │  trade_monitor.py   │
│  SQLAlchemy │          │  Background poller  │
│  Trade      │          │  SL / TP / Trailing │
│  Portfolio  │          │  stop execution     │
└─────────────┘          └────────────────────┘

┌─────────────────────────────────────────────────────┐
│               main.py  (MCP API Server)             │
│  FastMCP + REST endpoints · Stytch JWT auth         │
│  Rate limiting · CSRF · Security headers            │
└─────────────────────────────────────────────────────┘
```

---

## Project Files

| File | Purpose |
|------|---------|
| `tui.py` | Textual TUI dashboard — main interactive interface |
| `main.py` | FastMCP HTTP server with REST API for n8n/AI agents |
| `deriv_client.py` | Async WebSocket client for the Deriv API + technical indicators |
| `database.py` | SQLAlchemy ORM models (`Trade`, `Portfolio`) + `TradingRepository` |
| `trade_monitor.py` | Background service polling contract settlements, executing stops |
| `logger.py` | Structured logging setup with audit trail helpers |

---

## Setup

### 1. Install dependencies

```bash
uv sync
```

### 2. Environment variables

Create a `.env` file in the project root:

```env
# Deriv API credentials
DERIV_APP_ID=1089
DERIV_API_TOKEN=your_api_token_here

# MCP server (optional — only needed for main.py)
STYTCH_PROJECT_ID=your_project_id
STYTCH_SECRET=your_secret
STYTCH_DOMAIN=https://test.stytch.com
AUTH_ENABLED=true

# Trading limits (optional)
MAX_DAILY_LOSS=100
MAX_DAILY_TRADES=50

# Server (optional)
HOST=0.0.0.0
PORT=8000
```

### 3. Database

The SQLite database is created automatically on first run at the path set by `DATABASE_URL` (default: `sqlite:////app/data/trading_database.db`). Schema migrations run on startup.

---

## Running

### TUI Dashboard

```bash
python tui.py
```

### MCP API Server

```bash
python main.py
```

Server starts on `http://0.0.0.0:8000`.

### Trade Monitor (standalone)

```bash
python trade_monitor.py --interval 30
```

---

## TUI Dashboard

### Layout

```
┌─ Header (clock) ────────────────────────────────────────────┐
│ [Dashboard] [Trade History] [Place Trade]                   │
│                                                             │
│ ┌─ ACCOUNT ──┐ ┌─ PORTFOLIO STATS ──┐ ┌─ CONNECTION ──┐   │
│ │ Balance    │ │ Trades  Win Rate   │ │ ● LIVE        │   │
│ │ Currency   │ │ P&L                │ │ Refresh: AUTO │   │
│ └────────────┘ └────────────────────┘ └───────────────┘   │
│                                                             │
│ ┌─ MARKET DATA ─────────────────────────────────────────┐  │
│ │ Symbol  Price   Change        RSI(14)  SMA(14)        │  │
│ │ R_50    1234.5  ▲ +0.00012   [65.2]   1234.1         │  │
│ └───────────────────────────────────────────────────────┘  │
│                                                             │
│ ┌─ PRICE SPARKLINES ─────────────────────────────────────┐ │
│ │ R_50    ▁▂▃▄▅▆▇█▇▆▅▄▃▂▃▄▅▆▇                           │ │
│ │ R_100   ▂▃▄▃▂▃▄▅▆▇▆▅▄▃▂▁▂▃▄                           │ │
│ │ 1HZ50V  ▅▄▃▂▃▄▅▆▅▄▃▄▅▆▇▆▅▄▃                           │ │
│ └───────────────────────────────────────────────────────┘ │
│                                                             │
│ ┌─ OPEN TRADES  (Enter for detail) ─────────────────────┐  │
│ │ Trade ID  Symbol  Type  Amount  Entry    P&L          │  │
│ └───────────────────────────────────────────────────────┘  │
│                                                             │
│ ┌─ LOG ─────────────────────────────────────────────────┐  │
│ │ 14:23:01 TradeMonitor started                         │  │
│ └───────────────────────────────────────────────────────┘  │
│                                                             │
│ > command (type 'help')                                     │
└─ Footer (key hints) ────────────────────────────────────────┘
```

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `1` | Switch to Dashboard tab |
| `2` | Switch to Trade History tab |
| `3` | Switch to Place Trade tab |
| `Tab` / `Shift+Tab` | Cycle focus between widgets |
| `j` | Scroll focused table down |
| `k` | Scroll focused table up |
| `Enter` | Open trade detail modal (on Open Trades row) |
| `Escape` | Focus command bar |
| `r` | Refresh all data |
| `a` | Toggle auto-refresh on/off |
| `b` | Quick buy — pre-fills command bar with `place ` |
| `s` | Quick sell — pre-fills command bar with `sell ` |
| `Ctrl+L` | Clear activity log |
| `F1` | Show help in log |
| `q` | Quit |

### Command Bar

Type commands in the bottom input bar and press `Enter`.

| Command | Description |
|---------|-------------|
| `help` | Show all commands and key bindings |
| `place <SYM> <CALL\|PUT> <amount> [duration=5] [sl=N] [tp=N]` | Place a trade |
| `sell <contract_id>` | Sell/close an open contract |
| `watch <SYM>` | Add a symbol to the market table and sparklines |
| `unwatch <SYM>` | Remove a symbol from the market table and sparklines |
| `refresh` | Refresh all data panels |
| `clear` | Clear the activity log |

**Examples:**

```
place R_50 CALL 1.00 duration=5 sl=1233.0 tp=1236.0
place 1HZ50V PUT 2.50 duration=10
sell 123456789
watch BOOM500
unwatch R_100
```

### Trade Detail Modal

Press `Enter` on any row in the Open Trades table to open a full-screen overlay showing:

- Trade ID, Symbol, Type, Amount, Entry Price
- Current P&L and Status
- Stop Loss, Take Profit
- Trailing Stop Distance and current Trailing Stop Price
- Opened and Closed timestamps

Press `Enter` or `Escape` to close.

### Visual Indicators

**RSI coloring:**
- Red (`bold red`) — RSI > 70 (overbought)
- Yellow — RSI 30–70 (neutral)
- Green (`bold green`) — RSI < 30 (oversold)

**Change column:**
- `▲ +X.XXXXX` in green — price up
- `▼ X.XXXXX` in red — price down

**P&L cells:**
- `+$X.XX` in green — profitable
- `-$X.XX` in red — loss

**Type cells:**
- `CALL` in green
- `PUT` in red

**Sparklines:** Last 20 tick prices per watched symbol. Green = max value, red = min value.

**Connection status panel:** Shows `● LIVE` (green) when API is reachable or `● DISCONNECTED` (red) on failure. Subtitle bar shows `● LIVE | N open | AUTO | HH:MM:SS`.

### Auto-Refresh Intervals

| Data | Interval |
|------|---------|
| Balance / connection status | 30 s |
| Open trades + portfolio stats | 10 s |
| Market data + sparklines | 5 s |

Press `a` to pause all three timers simultaneously. Press `a` again to resume.

---

## MCP API Server

### Authentication

All endpoints (except `/health`, `/api/health`, `/.well-known/oauth-protected-resource`) require a Stytch JWT in the Authorization header:

```
Authorization: Bearer <session_jwt>
```

Obtain a token via `POST /api/create-test-token` with `{ "email": "...", "password": "..." }`.

Set `AUTH_ENABLED=false` in `.env` to disable auth for local testing.

### REST Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness check (no auth) |
| `GET` | `/api/health` | Health check (no auth) |
| `GET` | `/api/balance` | Account balance |
| `GET` | `/api/market-data/{symbol}` | Current tick for symbol |
| `GET` | `/api/candles/{symbol}?timeframe=1m&count=50` | OHLC candles (1m/5m/15m/1h) |
| `POST` | `/api/trade` | Place a trade |
| `POST` | `/api/trade/{contract_id}/sell` | Sell/close a contract |
| `GET` | `/api/trades` | Trade history (text) |
| `GET` | `/api/trades/list` | Trade history (JSON) |
| `GET` | `/api/trades/open` | Open trades with live prices |
| `POST` | `/api/trades/sync` | Manually sync trade statuses |
| `POST` | `/api/trades/check` | Trigger trade monitor check |
| `GET` | `/api/trades/summary` | Aggregated stats |
| `GET` | `/api/indicators/{symbol}?indicator=rsi&period=14` | SMA or RSI |
| `GET` | `/api/portfolio` | Portfolio analysis (text) |
| `GET` | `/api/portfolio/stats` | Portfolio stats + P&L history (JSON) |
| `GET` | `/api/symbols` | Active trading symbols |
| `GET` | `/api/trade-monitor/status` | Trade monitor running state |
| `POST` | `/api/create-test-token` | Get a test JWT |
| `GET` | `/api/auth/test` | Verify authentication |
| `GET` | `/.well-known/oauth-protected-resource` | OAuth metadata |

#### Place Trade Request Body

```json
{
  "symbol": "R_50",
  "amount": 1.00,
  "direction": "CALL",
  "duration": 5,
  "stop_loss": 1233.0,
  "take_profit": 1236.0,
  "trailing_stop_distance": 0.5
}
```

Allowed symbols: `R_10`, `R_25`, `R_50`, `R_75`, `R_100`, `1HZ10V`–`1HZ100V`, `JD10`–`JD100`, `BOOM300N/500/1000`, `CRASH300N/500/1000`.

### MCP Tools

| Tool | Description |
|------|-------------|
| `get_account_balance()` | Fetch live balance |
| `get_market_data(symbol)` | Current price for a symbol |
| `place_trade(symbol, amount, direction, duration)` | Place a binary contract |
| `get_trade_history()` | All trades for the current user |
| `calculate_technical_indicators(symbol, indicator, period)` | SMA or RSI |
| `analyze_portfolio_performance()` | Win rate, total P&L, balance |
| `get_active_symbols()` | List available trading symbols |

---

## Trade Monitor

`TradeMonitor` runs as a background service (inside the TUI and optionally in the API server). Every `poll_interval` seconds it:

1. Fetches the Deriv portfolio and profit table
2. Marks settled contracts as `closed` in the database
3. Checks all trades with active SL/TP/trailing stops against current prices
4. Executes sells when a stop level is triggered

Trailing stops move automatically as price moves in the favorable direction.

---

## Database Schema

```sql
CREATE TABLE trades (
    id                      INTEGER PRIMARY KEY,
    user_id                 VARCHAR NOT NULL,
    trade_id                VARCHAR NOT NULL UNIQUE,  -- Deriv contract ID
    symbol                  VARCHAR NOT NULL,
    trade_type              VARCHAR NOT NULL,          -- 'call' or 'put'
    amount                  FLOAT NOT NULL,
    entry_price             FLOAT NOT NULL,
    exit_price              FLOAT,
    profit_loss             FLOAT,
    status                  VARCHAR NOT NULL DEFAULT 'open',  -- 'open' | 'closed'
    created_at              DATETIME,
    closed_at               DATETIME,
    stop_loss               FLOAT,
    take_profit             FLOAT,
    trailing_stop_distance  FLOAT,
    trailing_stop_price     FLOAT,
    highest_price_seen      FLOAT
);

CREATE TABLE portfolios (
    id           INTEGER PRIMARY KEY,
    user_id      VARCHAR NOT NULL,
    balance      FLOAT NOT NULL DEFAULT 0.0,
    equity       FLOAT NOT NULL DEFAULT 0.0,
    margin       FLOAT NOT NULL DEFAULT 0.0,
    free_margin  FLOAT NOT NULL DEFAULT 0.0,
    updated_at   DATETIME
);
```

---

## Docker

```bash
docker build -t deriv-trading-bot .
docker run -p 8000:8000 --env-file .env deriv-trading-bot
```

---

## Build Log

A chronological record of how this project was built.

### Phase 1 — Initial Commit

**Commit: `5034b20` — Initial commit: Deriv trading bot**

Established the full project foundation in a single commit:

- **`deriv_client.py`** — Async WebSocket client wrapping the Deriv v3 API. Implemented `connect`, `authorize`, `get_account_balance`, `get_ticks`, `get_ticks_history`, `get_candles`, `place_contract`, `sell_contract`, `get_portfolio`, `get_profit_table`, `get_contract_status`. Added `TechnicalIndicators` class with `calculate_sma` and `calculate_rsi` (Wilder smoothing, 14-period default).

- **`database.py`** — SQLAlchemy ORM with `Trade` and `Portfolio` models. `TradingRepository` static methods: `create_trade`, `create_trade_with_sl_tp`, `get_open_trades`, `get_trades_by_user`, `get_trade_by_trade_id`, `update_trade_result`, `update_trailing_stop`, `get_trades_summary`, `get_trades_with_active_stops`, `update_portfolio`. Auto-migration on startup adds new columns to existing databases without data loss.

- **`trade_monitor.py`** — `TradeMonitor` background service. Polling loop checks open trades against Deriv portfolio and profit table, falls back to direct `get_contract_status` for trades missing from both. `check_stop_levels` fetches current prices per-symbol (grouped to minimize API calls) and executes sells when SL, TP, or trailing stop is triggered. Trailing stops ratchet as price moves favorably. Context manager (`async with monitor.running()`) for clean lifecycle management. Configurable poll interval, 3-retry logic with reconnect.

- **`logger.py`** — Structured logging with rotating file handler. Audit-trail helpers: `log_trade_placed`, `log_trade_closed`, `log_api_call`, `log_error`, `log_balance_update`, `log_audit_auth`, `log_audit_trade`.

- **`main.py`** — FastMCP HTTP server. JWT auth middleware using Stytch (signature verification pending JWKS). Rate limiting via `slowapi` (10 trades/min, 60 reads/min). Three security middleware layers: `RequestSizeLimitMiddleware` (100 KB default), `CSRFProtectionMiddleware` (Origin header validation), `SecurityHeadersMiddleware` (X-Content-Type-Options, X-Frame-Options, CSP, etc.). Symbol whitelist (Volatility, 1-second, Jump, Crash/Boom indices). Daily loss and trade count limits. All REST endpoints listed above. MCP tools for AI agent integration.

- **`tui.py`** — Initial Textual TUI. Three tabs: Dashboard, Trade History, Place Trade. Dashboard: account panel, portfolio panel, market data table (Symbol/Price/Change/RSI/SMA), open trades table, activity log. Command bar: `place`, `sell`, `watch`, `unwatch`, `refresh`, `clear`, `help`. Background workers with `@work(exclusive=True)` for balance, open trades, market data, trade history. 30/10/5-second auto-refresh intervals. TradeMonitor integration as a background worker.

---

### Phase 2 — Dependency Fix

**Commit: `05f95cd` — Add missing slowapi dependency**

Added `slowapi>=0.1.9` to `pyproject.toml`. The rate limiter was already implemented in `main.py` but the package was omitted from the dependency list, causing import errors on fresh installs.

---

### Phase 3 — TUI Enhancement

**File modified: `tui.py`** (no new files)

A focused enhancement pass covering four areas: navigation, visual polish, functionality, and sparklines.

#### 3.1 Planning

Read the existing `tui.py`, `database.py`, and `deriv_client.py` to understand current state and the exact field names and API return shapes needed. Verified import availability:

- `textual 8.0.0` confirmed present
- `Sparkline(data, *, min_color, max_color, ...)` constructor confirmed
- `VerticalScroll`, `ModalScreen`, `Rule`, `Timer` all confirmed importable
- `DataTable.RowSelected` event confirmed, with `event.row_key.value` providing the string key set via `add_row(..., key="...")`
- `TabbedContent.active` confirmed as a settable reactive
- `Timer.pause()` / `Timer.resume()` confirmed on objects returned by `set_interval()`

#### 3.2 New imports

```python
from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import (..., Rule, Sparkline, ...)
```

#### 3.3 `TradeDetailModal(ModalScreen)`

New class placed above `DerivTradingApp`. Takes a `Trade` ORM object. Inline CSS centers a 64-char-wide overlay with a double border. Displays 13 fields in `label: value` rows, including all SL/TP/trailing stop fields. `Binding("escape"/"enter", "dismiss")` on the modal class itself. Opened via `self.push_screen(TradeDetailModal(trade))` from `on_data_table_row_selected`.

#### 3.4 BINDINGS expansion

Added `1`/`2`/`3` for tab switching, `j`/`k` for table scroll, `Escape` for command bar focus, `a` for auto-refresh toggle, `b`/`s` for quick buy/sell, `Ctrl+L` for log clear. Kept `r`, `q`, `F1`.

#### 3.5 Instance state

Added to `__init__`:
- `_price_history: dict[str, list[float]]` — rolling 20-price buffer per symbol
- `_auto_refresh: bool` — current pause/resume state
- `_timer_balance`, `_timer_open`, `_timer_market: Optional[Timer]` — stored handles for `pause()`/`resume()`
- `_last_refresh: dict[str, str]` — timestamp strings for subtitle
- `_open_trade_row_map: dict[str, Trade]` — row key → Trade for modal lookup

#### 3.6 CSS

- `#status-panel` added at `width: 22; height: 6`
- `#top-row` height bumped from 5 → 6
- `#sparkline-panel` at `height: auto; min-height: 4; max-height: 8`
- `.sparkline-row`, `.sparkline-label`, `.sparkline-widget` classes for layout
- `TradeDetailModal.CSS` is inline on the modal class

#### 3.7 `compose()` changes

- `Vertical(id="dashboard")` → `VerticalScroll` so the dashboard scrolls when the terminal is too short
- Third `Static(id="status-panel")` added to the top row
- Sparkline panel with one `Horizontal` row per `DEFAULT_SYMBOLS`, each row containing a `Static` label + `Sparkline([], id=f"sparkline-{sym}", min_color="#cc4444", max_color="#44cc44")`
- Both DataTables now have `cursor_type="row"` so Enter fires `RowSelected`

#### 3.8 `on_mount()` changes

- Timer return values stored: `self._timer_balance = self.set_interval(30, ...)` etc.
- `#status-panel` and `#sparkline-panel` border titles set
- Open Trades border title updated to include `[dim](Enter for detail)[/dim]`
- `_set_connection_status(connected=False)` called immediately to initialize the panel

#### 3.9 Worker updates

**`refresh_balance`**: calls `_set_connection_status(True)` on success, `(False)` on error.

**`refresh_open_trades`**:
- Clears and rebuilds `_open_trade_row_map` on each refresh
- P&L uses `Text("+$X.XX", style="bold green")` / `Text("-$X.XX", style="bold red")`
- Type uses `Text("CALL", style="bold green")` / `Text("PUT", style="bold red")`
- `add_row(..., key=str(t.trade_id))` for stable row keys
- Calls `_update_sub_title()` after completion

**`refresh_market_data`**:
- Change column: `Text("▲ +X.XXXXX", style="bold green")` / `Text("▼ X.XXXXX", style="bold red")`
- RSI: `bold red` >70, `bold green` <30, `yellow` 30–70
- Updates `_price_history[symbol] = prices[-20:]`
- Queries `Sparkline` by ID and sets `.data = prices[-20:]`
- Calls `_update_sub_title()` after all symbols processed

**`refresh_history`**: same Rich Text coloring for P&L and Type columns.

#### 3.10 Action methods

| Method | Behavior |
|--------|---------|
| `action_switch_tab(tab_id)` | `self.query_one("#tabs", TabbedContent).active = tab_id` |
| `action_scroll_down_table()` | Calls `.action_scroll_down()` on focused `DataTable` |
| `action_scroll_up_table()` | Calls `.action_scroll_up()` on focused `DataTable` |
| `action_focus_command_bar()` | Focuses `#command-bar` Input |
| `action_clear_log()` | Calls `.clear()` on `#activity-log` RichLog |
| `action_toggle_auto_refresh()` | Flips `_auto_refresh`, pauses/resumes all three timers, logs state, calls `_update_sub_title()` |
| `action_quick_buy()` | Sets command bar value to `"place "`, focuses it, moves cursor to end |
| `action_quick_sell()` | Sets command bar value to `"sell "`, focuses it, moves cursor to end |

#### 3.11 Event handler

```python
def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
    if event.data_table.id != "open-trades-table":
        return
    trade = self._open_trade_row_map.get(event.row_key.value)
    if trade:
        self.push_screen(TradeDetailModal(trade))
```

Only fires for the open trades table; history table row clicks are ignored.

#### 3.12 Helper methods

**`_set_connection_status(connected)`**: Updates `#status-panel` content with `● LIVE` (green) or `● DISCONNECTED` (red) plus current auto-refresh mode.

**`_update_sub_title()`**: Sets `self.sub_title` to `"● LIVE | N open | AUTO | HH:MM:SS"`, derived from `_open_trade_row_map` length and `_auto_refresh` state.

#### 3.13 `watch` / `unwatch` updates

**`watch SYM`**: After adding to `_watched_symbols`, mounts a new `Horizontal` row into `#sparkline-panel` containing a label `Static` and `Sparkline` pre-seeded from `_price_history`. Allows real-time addition of symbols without restart.

**`unwatch SYM`**: After removing from `_watched_symbols`, pops from `_price_history`, then calls `.parent.remove()` on the sparkline widget to remove the entire row from the DOM.

#### 3.14 Verification

```
python -c "import tui; print('Import OK')"  →  Import OK
```

AST analysis confirmed all 29 methods and 2 classes present with zero missing items from the requirements checklist.

---

## Security Notes

- JWT signature verification is currently disabled (`verify_signature: False`). In production, implement JWKS fetch from Stytch to verify token signatures.
- The `AUTH_ENABLED=false` bypass is intended for local development only — never deploy with it disabled.
- Daily loss limits (`MAX_DAILY_LOSS`, `MAX_DAILY_TRADES`) are enforced server-side but not in the TUI's local command path.
