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

## Risk And Trade Management

- Frontend API base is configurable with `?api=http://127.0.0.1:8787` and is remembered in browser storage.
- Backend runtime uses the modular engine files in `backend/engines/`.
- Trade journal persists to `backend/data/trade_journal.json` or `BOT_JOURNAL_PATH`.
- Position sizing uses wallet equity, risk-per-trade %, stop-loss %, max allocation, and Bybit instrument qty rules.
- Risk guard blocks duplicate symbol positions and max-open-position overflow.
- Daily risk guard blocks entries after the daily loss cap or max trades/day is reached.
- Trade manager can move profitable positions to breakeven stop when the configured trigger is reached.
- Trade manager can take one-time partial profit with a reduce-only market close.
- Trade manager can arm Bybit trailing stops after the configured profit trigger.

## Safety

This project is configured for Bybit Demo Trading. Do not commit `.env`; use `.env.example` only.
