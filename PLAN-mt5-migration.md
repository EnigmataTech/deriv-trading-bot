# Plan — Migrate Trading Core from Deriv WS API → MetaTrader 5

**Why:** Deriv retired the legacy WS v3 API; the classic API-token page now redirects to the new (immature) API. The bot can no longer authorize. MT5 is stable, matches how Enigma trades (lot + entry + price SL/TP), supports synthetics, and produces clean training data.

**Guiding rule:** swap ONLY the broker layer (`deriv_client.py` → `mt5_client.py`). The bot's REST API surface (the contract with the TUI) and the edge/backend split stay the same. See [[edge-backend-split-progress]].

---

## Phase 0 — Discovery (DONE)

**Architecture stays:** VPS (stinger) = MT5 terminal (Wine+Xvfb) + bot (MT5-backed) → writes cluster Postgres. Cluster = Deriv-free read-only API serving TUIs from Postgres (no change). TUI reads cluster backend (no change). Chain: `bot (Linux py) → mt5linux RPyC → MT5-python (Wine) → MT5 terminal (Wine+Xvfb) → Deriv MT5 demo`.

**Broker interface to mirror** (`deriv_client.py` `DerivAPIClient`): `get_account_balance`, `get_active_symbols`, `get_contracts_for`, `get_ticks`, `get_ticks_history`, `get_candles(symbol,granularity,count)`, `place_contract`/`place_multiplier_contract`, `get_portfolio`, `get_contract_status`, `get_profit_table`, `sell_contract`. `TechnicalIndicators.calculate_*` are pure math — **KEEP unchanged**. `DerivTickStream` (prices/run) → replace with MT5 tick polling.

**Data model is already MT5-shaped** (database.py): `Trade` has `entry_price`, `stop_loss`/`take_profit` (prices), `trailing_*`; `amount` → reused as **lot size**; add `mt5_ticket`. `Portfolio` = `balance/equity/margin/free_margin` = MT5 `account_info()` exactly. Identity stays `user_id=hermes_agent`.

**Symbols** (bot → MT5 name, CONFIRM against live terminal in Phase 1): `R_10→"Volatility 10 Index"`, `R_25→"Volatility 25 Index"`, `R_50→"Volatility 50 Index"`, `R_75→"Volatility 75 Index"`, `R_100→"Volatility 100 Index"`, `1HZ10V→"Volatility 10 (1s) Index"`, `1HZ75V→"Volatility 75 (1s) Index"`, `1HZ100V→"Volatility 100 (1s) Index"`, etc.

**VPS capacity confirmed:** Ubuntu 24.04 x86_64, 2 CPU / 8 GB (6.4 free), `Xvfb`+`xvfb-run` present, Wine NOT yet installed.

**Anti-patterns:** don't rewrite the REST endpoints or TUI; don't give the cluster bot MT5/broker access (stays read-only); don't reimplement indicators; `MetaTrader5` pip is Windows-only — never `pip install` it in Linux-native venv (use the Wine-python bridge).

**USER DEPENDENCY:** create a **Deriv MT5 demo (synthetic indices)** account → provide login / password / server (e.g. `Deriv-Demo`).

---

## Phase 1 — Stand up MT5 + Wine on the VPS (infra)
- `apt install wine` (+ deps via winetricks: corefonts, vcrun). Create a dedicated Wine prefix.
- Download Deriv MT5 installer, silent-install into the prefix.
- Headless run: MT5 terminal under `xvfb-run` as a `systemctl --user` service (`mt5-terminal.service`), auto-login to the demo account, **AutoTrading/Algo enabled** (terminal config or `/portable` + config file).
- Install Wine-python + `MetaTrader5` package inside the prefix; run the **`mt5linux`** RPyC server as a `systemctl --user` service (`mt5-bridge.service`). Linux venv gets the `mt5linux` client.
- **Verify:** from the Linux venv, `mt5linux` connects → `account_info()` returns demo balance; `symbol_info("Volatility 75 Index")` resolves; `copy_rates_from_pos` returns OHLC. Confirm the real MT5 symbol names (update the mapping table).
- **Anti-pattern guard:** keep AutoTrading ON or `order_send` returns `AutoTrading disabled`.

## Phase 2 — Write `mt5_client.py` (new broker layer)
Mirror the interface above, backed by MT5 (via `mt5linux` client). Map calls:
- `get_account_balance()` → `account_info()` → `{balance, equity, margin, free_margin, currency}`
- `get_candles(sym,gran,count)` → `copy_rates_from_pos(mt5_sym, tf_map[gran], 0, count)` → OHLC list
- ticks/`prices` → `symbol_info_tick(mt5_sym)` (poll loop replacing `DerivTickStream`)
- `get_open_positions()` → `positions_get()` → map to the bot's open-trade shape
- `place_order(sym, side, lot, sl_price, tp_price, entry=None)` → `order_send()` market (or pending if `entry` set), SL/TP as prices
- `close_position(ticket)` → `order_send()` opposite by ticket
- `get_profit_table()`/history → `history_deals_get()`
- symbol mapping table (bot↔MT5) lives here.
- **Verify:** a small script exercises each method against the live demo terminal (balance, candles, place+close a 0.01-lot Vol 75 trade, see it in `positions_get`/history).

## Phase 3 — Wire `main.py` to MT5 + adapt trade flow
- Add `BROKER` env (`deriv`|`mt5`); `get_broker_client()` returns `mt5_client` when `BROKER=mt5`. Route `_do_get_balance`, market-data/candles, place-trade, and `sell` paths through it.
- Trade placement endpoint: accept lot + SL/TP prices (+ optional entry). Persist `amount`(=lot), `entry_price`, `stop_loss`/`take_profit` (prices), `mt5_ticket`.
- `trade_monitor.py`: reconcile open trades via `positions_get()` + `history_deals_get()` (replace `get_portfolio`/`get_profit_table`); settle `exit_price`/`profit_loss`/`closed_at` from MT5.
- Postgres migration: `ALTER TABLE trades ADD COLUMN mt5_ticket bigint;` (idempotent).
- **Verify:** REST place-trade → order appears in MT5 terminal AND Postgres `trades`; monitor closes it on SL/TP; `portfolios` snapshot tracks `account_info()`.

## Phase 4 — TUI label tweaks
- Trade view: show **lot size + entry + SL/TP prices** (instead of multiplier/stake). Balance/chart/signals views unchanged.
- **Verify:** TUI (this machine) against the cluster backend renders trades correctly.

## Phase 5 — Resilience + cleanup
- Carry over edge/backend Phase 4: VPS bot buffers writes to local SQLite if cluster Postgres unreachable, replays on reconnect.
- Gate/retire `deriv_client.py` behind `BROKER=deriv` (keep for reference, don't delete yet).
- Fix the pre-existing `shutdown_handler` bug (main.py:2709 `run_until_complete` in running loop).

## Final — Verification
- MT5 terminal headless + bridge stable across a VPS reboot (services enabled).
- REST place→monitor→close cycle works end-to-end on demo; data lands in cluster Postgres.
- TUI shows live balance + lot-based trades.
- Cluster bot still Deriv/MT5-free read-only.

## Sequencing / risk
Phase 1 is the gating risk (Wine + headless MT5 stability) — prove it before coding. Phases 2–3 are the core. 4–5 harden. Multi-session effort; demo-only so no money risk. Biggest unknowns: headless MT5 demo session stability, real synthetic symbol names + min lot sizes (confirm in Phase 1).
