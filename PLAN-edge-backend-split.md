# Plan — Deriv Bot Edge/Backend Split

**Goal:** VPS "Stinger" = always-on trader (holds Deriv token, trades 24/7, Hermes/Telegram brain). Cluster = Deriv-free backend/data layer (Postgres source of truth + read-only API serving TUIs). Survives cluster/power outage on the trading side. Demo account; 24/7 data → future training model.

**Guiding rule:** the Deriv demo token authorizes ONE connection at a time → only the VPS bot holds it. The cluster bot must NOT try to authorize.

---

## Phase 0 — Discovery (DONE — Allowed APIs / facts)

Code: local repo `/home/enigma/Projects/deriv-trading-bot`, image `enigmata/deriv-trading-bot:latest`, Flux-managed Deployment (single replica, `Recreate`), secret `deriv-trading-bot-secrets` (NOT Flux-managed — live patch sticks).

- `_do_get_balance()` — **main.py:452** — `await client.get_account_balance()` (live Deriv); on success calls `TradingRepository.update_portfolio(user_id, balance, equity, margin, free_margin)` (writes the snapshot). On error returns `f"Error: {response['error']['message']}"`.
- `/api/balance` GET — **main.py:1219** `api_get_balance` → `_do_get_balance()`.
- `TradingRepository.update_portfolio(user_id, balance, equity, margin, free_margin)` — **database.py:120** (writes `portfolios`).
- `TradingRepository.get_portfolio(user_id) -> Portfolio|None` — **database.py:146** (reads `portfolios`: balance/equity/margin/free_margin/updated_at).
- `/api/portfolio/stats` GET — **main.py:2277** — Postgres-only trade stats (no balance).
- `require_auth()` — **main.py:~248** — auth-disabled identity now `MCP_AGENT_USER_ID` (`hermes_agent`).
- TUI `tui.py`: `API_BASE = os.getenv("TRADING_API_URL", "http://trading.enigmata.local")` (**tui.py:48**, dead default); balance via `api_get("/api/balance")` (**tui.py:595**); 30s refresh (**tui.py:535**).
- Deriv ticks/`active_symbols`/market-data do NOT require authorize → cluster bot keeps serving signals with NO token.
- Existing infra: reverse tunnel `pg-revtunnel-stinger.service` on k3s-control-01 → stinger `127.0.0.1:5432`→PG ClusterIP, `127.0.0.1:8001`→bot ClusterIP. VPS bot `.env` `DATABASE_URL=postgresql://trading:***@127.0.0.1:5432/trading`.

**Anti-patterns:** do NOT give the cluster bot a Deriv token; do NOT invent new DB methods (use existing `get_portfolio`/`update_portfolio`); do NOT change `/api/portfolio/stats` to call Deriv; do NOT rely on `trading.enigmata.local` (ingress gone).

---

## Phase 1 — Cluster bot becomes Deriv-free; balance served from Postgres snapshot

**What to implement (main.py:452, `_do_get_balance`):** add a no-token branch BEFORE the Deriv call. Pattern (mirror existing `get_portfolio` usage at main.py:923):
```python
async def _do_get_balance() -> str:
    if not os.getenv("DERIV_API_TOKEN"):           # cluster read-only mode
        user_id = get_user_id()
        p = TradingRepository.get_portfolio(user_id)
        if p:
            return f"Balance: {p.balance} USD (snapshot {p.updated_at:%Y-%m-%d %H:%M} UTC)"
        return "Error: No balance snapshot available yet"
    client = await get_deriv_client()
    ... # unchanged live path
```
**Then:** remove `DERIV_API_TOKEN` from the cluster secret: `kubectl patch secret deriv-trading-bot-secrets --type=json -p '[{"op":"remove","path":"/data/DERIV_API_TOKEN"}]'` (secret is not Flux-managed). Rebuild/push image (`docker build --network=host -t enigmata/deriv-trading-bot:latest .` → push → `kubectl rollout restart`).

**Verify:** cluster `/api/balance` returns `{"success":true,"data":"Balance: <n> USD (snapshot ...)"}` (NOT "Please log in"); `/api/signals` still 200 (market data unaffected); VPS bot unchanged (still has token, still trades).
**Anti-pattern guard:** keep the live path byte-identical; only ADD the no-token branch.

## Phase 2 — VPS bot keeps the snapshot fresh

The snapshot only updates when `_do_get_balance()` runs. Confirm/ensure the VPS bot refreshes it on a timer (TradeMonitor loop is the natural home — it already runs every 30s, see `ENABLE_TRADE_MONITOR`/`TRADE_MONITOR_INTERVAL`).
**What to implement:** in the VPS bot's monitor tick, call `_do_get_balance()` (or directly `update_portfolio`) each cycle so `portfolios.updated_at` stays current.
**Verify:** on the VPS, `portfolios.updated_at` for `hermes_agent` advances ~every 30s; cluster `/api/balance` snapshot reflects it within a cycle.

## Phase 3 — TUI reads from the cluster backend (reachability + balance source)

With Phase 1, the cluster bot's `/api/balance` already returns the snapshot, so **tui.py needs no endpoint change** — only a reachable `TRADING_API_URL`.
**What to implement:** set `TRADING_API_URL` (this machine `.env`) to a reachable cluster backend. Recommended (stable, ACL-independent): a small `systemctl --user` SSH forward `localhost:8001→cluster bot` via control-01 (mirror the VPS reverse-tunnel pattern), then `TRADING_API_URL=http://localhost:8001`. Repeat on flipside.
**Verify:** TUI on this machine shows balance updating every 30s and a populated trades/P&L view.
**Anti-pattern guard:** don't point at `trading.enigmata.local` or the dead operator IP `100.126.66.12`.

## Phase 4 — Resilience: VPS bot tolerates cluster Postgres outage

So 24/7 trading survives a cluster/power outage.
**What to implement:** in the VPS bot's DB layer (`database.py`), wrap writes so that when the tunneled Postgres is unreachable, trades buffer to a local SQLite spool and replay (idempotent on `trade_id` UNIQUE) when connectivity returns. Keep reads best-effort.
**Verify:** stop `pg-revtunnel-stinger` → place a trade on the VPS → it still records locally; restart tunnel → row appears in cluster Postgres with no duplicate.

## Phase 5 — (sub-goal) Capture decision-time features per trade

For training quality: persist the signal/indicator snapshot (RSI/MACD/composite) AT placement, not just entry/exit/P&L.
**What to implement:** add a JSON/`features` column (or sibling table) populated in the trade-placement path (`create_trade_with_sl_tp`, main.py:565/1445) from the same signal the bot acted on.
**Verify:** new trades carry a non-null features payload; backfill not required.

---

## Final Phase — Verification
- Cluster `/api/balance` = Postgres snapshot, never "Please log in"; `/api/signals` live.
- VPS bot still the sole Deriv-authorized trader; snapshot fresh.
- TUI (this machine + flipside) shows live-updating balance + trades from the cluster backend.
- Tunnel-down test: VPS keeps trading + buffers; replays clean on reconnect.
- `grep -rn "trading.enigmata.local\|100.126.66.12" tui.py .env` → no live references.

## Sequencing / churn control
Phase 1 (fixes the TUI balance + stops the cluster bot fighting for the token) → Phase 3 (TUI reachable) gives immediate working observability. Phases 2, 4, 5 harden it. One image rebuild covers Phase 1 (+ Phase 5 if batched).
