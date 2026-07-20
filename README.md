# Bybit Demo Intraday Trading Bot

Demo-only intraday trading bot control center with live Bybit demo data, multi-timeframe signal engines, risk guard, SL/TP, kill switch, and a two-page dashboard/control-center UI.

## Structure

```text
frontend/
  index.html

backend/
  server.py
  .env.example
  engines/
    bot_engine.py
    market_data.py
    indicators.py
    strategies.py
    router.py
    risk.py
    trade_management.py
    journal.py
```

## Run

1. Copy `backend/.env.example` to `backend/.env`.
2. Add your Bybit Demo API key and secret.
3. Start backend:

```powershell
cd backend
python server.py
```

If normal Python is unavailable, use the bundled Codex Python path from your machine.

4. Open `frontend/index.html`.

## Render Deploy

The repo includes Render-compatible root files:

- `requirements.txt` for the default Render build command.
- `render.yaml` with `python backend/server.py` as the start command.
- `app/main.py` supports Render dashboards that still use `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
- The backend binds to `0.0.0.0:$PORT` and serves the frontend at `/`.

Set these Render environment variables before live demo testing:

- `BYBIT_API_KEY`
- `BYBIT_API_SECRET`
- `BYBIT_BASE_URL=https://api-demo.bybit.com`

## Engines

Bot Engine V2 has seven layers:

- Market Data Engine
- Indicator Engine
- Strategy Engine
- Signal Router Engine
- Risk Engine
- Trade Management Engine
- Journal/Monitoring Engine

The working backend is `backend/server.py`. The `backend/engines/` folder contains the separated engine modules for a clean GitHub project structure and future refactor.

## Frontend Pages

- Dashboard
- Scanner & Signals
- Active Trades & Trade Journal
- Strategy & Performance Analytics
- Backtest / Paper Replay
- Settings & System Health / Risk Status

## Strategy V3

- Trend Following EMA Crossover
- Support/Resistance Breakout
- RSI Divergence
- VWAP Bounce
- Opening Range Breakout

Router modes:

- Balanced: one actionable engine vote is enough for demo execution.
- Conservative: two matching engine votes and no opposite vote.
- Aggressive demo: fastest majority/strongest engine signal.

The bot refreshes its scan universe every 10 minutes from the Bybit linear USDT top-gainer list, then scans the top 10 symbols for the best live signal. If the exchange ticker request fails, it falls back to BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT, DOGEUSDT, ADAUSDT, AVAXUSDT, LINKUSDT, and LTCUSDT.

Scanner filters remove low-turnover pairs, wide-spread pairs, weak momentum, and overextended gainers before the signal engine votes.

Backtest / Paper Replay uses live Bybit candles only. It can replay the last 24h or 7d, count all five strategy votes, estimate trades from router decisions, and score estimated win/loss without placing live or demo orders.

## Risk And Trade Management

- Frontend API base is configurable with `?api=http://127.0.0.1:8787` and is remembered in browser storage.
- Backend runtime uses the modular engine files in `backend/engines/`.
- Trade journal persists to `backend/data/trade_journal.json` or `BOT_JOURNAL_PATH`.
- Position sizing uses wallet equity, risk-per-trade %, stop-loss %, max allocation, Bybit instrument qty step, min order qty, min notional, and max order qty.
- Risk guard blocks duplicate symbol positions and max-open-position overflow.
- Daily risk guard blocks entries after the daily loss cap or max trades/day is reached.
- Trade manager can move profitable positions to breakeven stop when the configured trigger is reached.
- Trade manager can take one-time partial profit with a reduce-only market close after validating exchange min notional.
- Trade manager can arm Bybit trailing stops after the configured profit trigger.
- Execution guard blocks duplicate same-symbol entries, opposite/reverse entries, and max-open-position overflow before sending an order.
- Order lifecycle records signal, guard, order status, SL/TP protection, and final execution state for the dashboard.
- Dry-check position sizing without placing an order: `GET /api/bot/sizing?symbol=BTCUSDT`.

## Safety

This project is configured for Bybit Demo Trading. Do not commit `.env`; use `.env.example` only.
