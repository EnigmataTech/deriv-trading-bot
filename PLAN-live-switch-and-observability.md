# Plan: Live-account switch + observability-focused TUI

> Status: **APPROVED, not yet started** (planned 2026-06-25). Implementation deferred per user.
> Context: trade review on 2026-06-25 showed net +$298 over 41 trades is one outlier
> (+$295 manual Vol25 call); strip the outlier and it's ~breakeven (+$2.67). The
> deterministic `_autotrade_loop` had negative expectancy (−$18.84, 30% win) and is
> OFF. Hermes cron scanner is the sole intended auto-trader. User is going
> autonomous-only (no manual trades) and wants ~50 trades / ~2 weeks before re-review.

## Decisions locked in
- **Live switch:** config flag + restart; mode always verified from MT5 itself (not trusted from config).
- **Reasoning feed:** full per-scan log (incl. `[SILENT]`), which requires changes to the Hermes agent on stinger too.
- **Scope when started:** Phase 1 first.

---

## Codebase realities (grounding, verified 2026-06-25)
- **No demo/live concept in code today.** MT5 connects with whatever creds are in
  `~/.mt5creds` / `MT5_LOGIN|PASSWORD|SERVER` on stinger. `mt5_client.py:_load_creds()`
  (lines ~78-91) loads them; env overrides file.
- **Critical wrinkle:** `MT5Client._connect_sync()` (mt5_client.py ~109-127) first calls
  `mt5.initialize()` which **attaches to the bridge-launched terminal that is already
  logged in** via `mt5-startup.ini`. Creds are only a *fallback* when initialize fails.
  So a robust switch MUST force `mt5.login(login, password, server)` to the selected
  account after init, and verify via `account_info().trade_mode` (0=demo, 2=real).
- **Manual trading lives entirely in the TUI "Trade [2]" tab** (tui.py ~424-490):
  multiplier/binary mode buttons, quick-trade buttons, amount/duration/SL/TP inputs,
  "Place Trade", plus a "✕ Close Position" button in `TradeDetailModal` (~211).
  The `/api/trade*` endpoints in main.py stay (Hermes uses them) — TUI-only removal.
- **Chart markers already exist** (tui.py ~1009-1090): ▲/▼ entry arrows, ● win/loss
  exits, cyan open-position line, orange live line, placed via `plt.text` at candle
  *slots*. Plotext can't do a true time axis, so sub-candle precision is impossible.
- **Observability gap:** only `Trade.reason` is persisted, and only when a trade FIRES.
  Scanner runs every 10 min but ~90% of scans are `[SILENT]` → zero record. The TUI
  "agent-log" (tui.py ~898-948) just renders `/api/trades/agent` (fired trades only).
- **Trade model** (database.py ~109-133) already has entry/exit/pnl/sl/tp/mt5_ticket/reason.
- Hermes agent is a SEPARATE repo on stinger: `~/.hermes/hermes-agent` (cron scanner,
  job id `9741f3e2e6d8`, scans R_75,R_100,R_25,R_10,1HZ50V every 10m, Score>=+2/<=-2).

---

## Phase 1 — Demo/Live account switching (safety-first)
Goal: one deliberate, verifiable switch; impossible to not know which account is live.

**`mt5_client.py`**
- Add `MT5_ACCOUNT_MODE` (`demo`|`live`, default `demo`). Load two cred blocks
  (`MT5_DEMO_*`, `MT5_LIVE_*`) from `~/.mt5creds` / env.
- After `initialize()`, **force `mt5.login(login,password,server)` for the selected mode** —
  don't trust the bridge terminal's pre-logged-in default.
- Read back `account_info().trade_mode` and **assert it matches** the requested mode.
  If `MODE=live` but `trade_mode != 2` (real) → refuse to trade, log loudly. Guard
  protects both directions (thought-demo-was-live AND thought-live-was-demo).
- Expose `account_mode` + `trade_mode` (login #, server, currency) on the client.

**`main.py`**
- New `GET /api/account/mode` → `{mode, trade_mode, login, server, currency, balance}`.
- Startup banner: `TRADE MODE: LIVE (real money) — acct 5692xxxx @ Deriv-Server`.
- Keep `_autotrade_loop` OFF (sole-trader-Hermes decision). Live switch applies to
  whichever engine places orders.

**`.env` (stinger):** `MT5_ACCOUNT_MODE=demo` + two cred blocks. Switch = edit + backup
`.env` + `systemctl --user restart deriv-trading-bot.service`.

**Risk note:** add a position-size sanity clamp that re-reads on live mode so demo lot
sizes don't carry into a smaller live account unchecked (ties into existing
`AUTOTRADE_MAX_RISK_PCT` affordability gate). Live $20 account → only Vol 75 viable.

---

## Phase 2 — Strip manual trading from the TUI
- Delete the "Trade [2]" tab's manual order entry (mode buttons, quick-trade buttons,
  amount/duration/SL/TP inputs, "Place Trade") + their `on_button_pressed` handlers.
- Remove "✕ Close Position" button + handler from `TradeDetailModal` (read-only).
- Keep `/api/trade*` endpoints server-side (Hermes uses them).
- Repurpose tab **[2]** as the new **"Agent"** observability tab (Phase 4).

---

## Phase 3 — Chart: MetaTrader-style entry/exit (improve what exists)
- **Entry→exit connector** line colored by win/loss (green/red) — the MT5 "deal" line.
- **SL/TP bands:** faint red (SL) / green (TP) guides for the selected trade.
- **Win/loss labels:** `+$x.xx`/`-$x.xx` at each closed exit via `plt.text`.
- **Honest constraint:** plotext aligns to candle slots, not a true time axis — document,
  don't fake. Pixel-perfect MT5 chart = a web frontend (out of scope unless flipside revisited).

---

## Phase 4 — Agent reasoning stream (the big one)
Persist EVERY scan so silent activity is visible.

**`database.py`:** new `AgentActivity` table — `id, ts, event_type (scan|signal|trade|error|heartbeat),
symbol, score, decision, detail (text), trade_id (nullable)`.

**Hermes agent (stinger `~/.hermes/hermes-agent` — SEPARATE repo, remote deploy):**
- After each 10-min scan, POST a structured activity record (per-symbol score + threshold
  decision + overall `[SILENT]`/trade outcome) to the new bot endpoint.

**`main.py`:** `POST /api/agent/activity` (Hermes writes), `GET /api/agent/activity?limit=N`
(TUI reads). Start with existing 5s polling; SSE/long-poll later if needed.

**TUI ("Agent" tab [2]):**
- Live streaming `RichLog` of scans: ts, symbols, per-symbol scores, decision, why.
- Win/loss summary strip (today's W/L, net, streak) reusing `/api/portfolio/stats`.

---

## Suggested sequencing
1. Phase 1 (live switch) — highest value, self-contained, test on DEMO first.
2. Phase 2 + Phase 4-TUI (gut manual tab, build Agent tab).
3. Phase 4-backend + Hermes emitter + deploy.
4. Phase 3 (chart polish) — lowest risk, last.

## Open items to confirm before coding
- Validate the whole switch on the demo account first (force-login + trade_mode assertion),
  flip to live only while watching.
- Phase 4 needs edits on stinger's `~/.hermes/hermes-agent` + redeploy of that agent
  (outside this git repo) — confirm before touching.
