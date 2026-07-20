import asyncio
import json
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND_INDEX = ROOT / "frontend" / "index.html"
sys.path.insert(0, str(BACKEND))

import server as bot  # noqa: E402


def _json_bytes(payload):
    return json.dumps(payload, default=str).encode("utf-8")


async def _send_json(send, status, payload):
    body = _json_bytes(payload)
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"access-control-allow-origin", b"*"),
        ],
    })
    await send({"type": "http.response.body", "body": body})


async def _send_html(send, html):
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"text/html; charset=utf-8"),
            (b"cache-control", b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": html.encode("utf-8")})


async def _read_json(receive):
    body = b""
    more = True
    while more:
        event = await receive()
        if event["type"] != "http.request":
            break
        body += event.get("body", b"")
        more = event.get("more_body", False)
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


def _query(scope):
    return dict(urllib.parse.parse_qsl(scope.get("query_string", b"").decode("utf-8")))


def _bot_status():
    with bot.BOT_LOCK:
        payload = dict(bot.BOT_STATE)
        payload["engineOverview"] = bot.get_bot_engine().overview()
        payload["universe"] = bot.top_gainer_universe()
        payload["dailyRisk"] = bot.daily_risk_report(payload)
        payload["scanSeconds"] = bot.BOT_SCAN_SECONDS
        payload["topGainerRefreshSeconds"] = bot.TOP_GAINER_REFRESH_SECONDS
    return {"ok": True, "bot": payload}


def _scanner(query):
    universe = bot.top_gainer_universe(force=query.get("forceUniverse", "0") in ("1", "true", "yes"))
    symbols = query.get("symbols", ",".join(universe["symbols"]))
    interval = query.get("interval", "15")
    mode = bot.normalize_mode(query.get("mode", "balanced"))
    market_rows = {row.get("symbol"): row for row in universe.get("rows", [])}
    rows = []
    for symbol in [item.strip().upper() for item in symbols.split(",") if item.strip()]:
        signal, reason, votes, router, indicators, engine_status = bot.evaluate_signal(symbol, interval, mode)
        market = market_rows.get(symbol, {})
        rows.append({
            "symbol": symbol,
            "signal": signal,
            "reason": reason,
            "changePct": market.get("changePct"),
            "turnover24h": market.get("turnover24h"),
            "spreadPct": market.get("spreadPct"),
            "engineVotes": votes,
            "router": router,
            "indicators": indicators,
            "engineStatus": engine_status,
            "score": bot.signal_score({"signal": signal, "engineVotes": votes, "router": router}),
        })
    rows.sort(key=lambda row: row["score"], reverse=True)
    return {
        "ok": True,
        "interval": interval,
        "mode": mode,
        "rows": rows,
        "universe": universe,
        "scanSeconds": bot.BOT_SCAN_SECONDS,
        "topGainerRefreshSeconds": bot.TOP_GAINER_REFRESH_SECONDS,
    }


def _start_bot(payload):
    universe = bot.top_gainer_universe(force=True)
    symbol = str(payload.get("symbol") or universe["symbols"][0] or "BTCUSDT").upper()
    interval = str(payload.get("interval", "5"))
    mode = bot.normalize_mode(payload.get("mode", "balanced"))
    with bot.BOT_LOCK:
        bot.BOT_STATE.update({
            "enabled": True,
            "symbol": symbol,
            "interval": interval,
            "qty": str(payload.get("qty", "0.001")),
            "maxAllocationUsdt": max(1, float(payload.get("maxAllocationUsdt", 250))),
            "riskPerTradePct": max(0.01, float(payload.get("riskPerTradePct", 0.5))),
            "maxOpenPositions": max(1, int(payload.get("maxOpenPositions", 1))),
            "dailyLossCapUsdt": max(0, float(payload.get("dailyLossCapUsdt", 25))),
            "maxTradesPerDay": max(1, int(payload.get("maxTradesPerDay", 5))),
            "breakevenEnabled": payload.get("breakevenEnabled", True) is not False,
            "breakevenTriggerPct": max(0.1, float(payload.get("breakevenTriggerPct", 0.6))),
            "partialTpEnabled": payload.get("partialTpEnabled", True) is not False,
            "partialTpTriggerPct": max(0.1, float(payload.get("partialTpTriggerPct", 1.0))),
            "partialTpClosePct": max(1, min(100, float(payload.get("partialTpClosePct", 50)))),
            "trailingStopEnabled": payload.get("trailingStopEnabled", True) is not False,
            "trailingStopTriggerPct": max(0.1, float(payload.get("trailingStopTriggerPct", 0.8))),
            "trailingStopDistancePct": max(0.05, float(payload.get("trailingStopDistancePct", 0.35))),
            "stopLossPct": float(payload.get("stopLossPct", 0.8)),
            "takeProfitPct": float(payload.get("takeProfitPct", 1.6)),
            "cooldownSeconds": max(60, int(payload.get("cooldownSeconds", 300))),
            "mode": mode,
            "autoPick": True,
            "scanSymbols": list(universe["symbols"]),
            "symbolSource": universe["source"],
            "selectedSignalSymbol": symbol,
            "universe": universe,
            "lastReason": f"Auto trader started in {mode} mode with top-gainer scan.",
        })
    bot.ensure_bot_thread()
    return {"ok": True, "bot": bot.bot_tick()}


def _stop_bot():
    with bot.BOT_LOCK:
        bot.BOT_STATE.update({"enabled": False, "lastReason": "Auto trader stopped by user."})
        status = dict(bot.BOT_STATE)
    return {"ok": True, "bot": status}


def _manage_positions():
    with bot.BOT_LOCK:
        state = dict(bot.BOT_STATE)
    result = bot.manage_open_positions(state)
    with bot.BOT_LOCK:
        bot.BOT_STATE.update({"tradeManagement": result})
        status = dict(bot.BOT_STATE)
    return {"ok": result.get("ok", False), "result": result, "bot": status}


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    method = scope["method"].upper()
    path = scope["path"]
    query = _query(scope)

    if method == "OPTIONS":
        await send({
            "type": "http.response.start",
            "status": 204,
            "headers": [
                (b"access-control-allow-origin", b"*"),
                (b"access-control-allow-methods", b"GET,POST,OPTIONS"),
                (b"access-control-allow-headers", b"Content-Type"),
            ],
        })
        await send({"type": "http.response.body", "body": b""})
        return

    try:
        if method == "GET" and path in ("/", "/index.html", "/app"):
            html = FRONTEND_INDEX.read_text(encoding="utf-8")
            html = html.replace('const apiParam = new URLSearchParams(window.location.search).get("api");', 'const apiParam = new URLSearchParams(window.location.search).get("api") || window.location.origin;')
            await _send_html(send, html)
            return
        if method == "GET" and path == "/api/health":
            cfg = bot.config()
            await _send_json(send, 200, {"ok": True, "exchange": "bybit", "baseUrl": cfg["base_url"], "hasApiKey": bool(cfg["api_key"]), "hasApiSecret": bool(cfg["api_secret"])})
            return
        if method == "GET" and path == "/api/bybit/ticker":
            await _send_json(send, 200, bot.public_bybit_get("/v5/market/tickers", {"category": "linear", "symbol": query.get("symbol", "BTCUSDT").upper()}))
            return
        if method == "GET" and path == "/api/bybit/kline":
            candles, message = bot.fetch_candles(query.get("symbol", "BTCUSDT").upper(), query.get("interval", "5"))
            await _send_json(send, 200, {"ok": bool(candles), "symbol": query.get("symbol", "BTCUSDT").upper(), "interval": query.get("interval", "5"), "candles": candles or [], "message": message})
            return
        if method == "GET" and path == "/api/bybit/wallet":
            await _send_json(send, 200, bot.bybit_request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"}))
            return
        if method == "GET" and path == "/api/bybit/positions":
            await _send_json(send, 200, bot.bybit_request("GET", "/v5/position/list", {"category": "linear", "symbol": query.get("symbol", "BTCUSDT").upper()}))
            return
        if method == "GET" and path == "/api/bybit/open-orders":
            await _send_json(send, 200, bot.bybit_request("GET", "/v5/order/realtime", {"category": "linear", "symbol": query.get("symbol", "BTCUSDT").upper()}))
            return
        if method == "GET" and path == "/api/bot/status":
            await _send_json(send, 200, await asyncio.to_thread(_bot_status))
            return
        if method == "GET" and path == "/api/bot/sizing":
            with bot.BOT_LOCK:
                state = dict(bot.BOT_STATE)
            sizing = bot.calculate_position_sizing(query.get("symbol", "BTCUSDT").upper(), state)
            await _send_json(send, 200, {"ok": bool(sizing.get("ok")), "symbol": query.get("symbol", "BTCUSDT").upper(), "sizing": sizing})
            return
        if method == "GET" and path == "/api/bot/universe":
            await _send_json(send, 200, {"ok": True, "universe": bot.top_gainer_universe(force=query.get("force", "0") in ("1", "true", "yes"))})
            return
        if method == "GET" and path == "/api/bot/engine":
            await _send_json(send, 200, {"ok": True, "engine": bot.get_bot_engine().overview()})
            return
        if method == "GET" and path == "/api/bot/journal":
            limit = max(1, min(500, int(query.get("limit", "100"))))
            engine = bot.get_bot_engine()
            await _send_json(send, 200, {"ok": True, "journal": engine.journal.recent(limit), "journalPath": str(engine.journal.path)})
            return
        if method == "GET" and path == "/api/bot/scanner":
            await _send_json(send, 200, await asyncio.to_thread(_scanner, query))
            return
        if method == "GET" and path == "/api/bot/replay":
            payload = await asyncio.to_thread(bot.replay_strategy_quality, query.get("symbol", "BTCUSDT").upper(), query.get("horizon", "24h"), bot.normalize_mode(query.get("mode", bot.BOT_STATE.get("mode", "balanced"))), bot.numeric(query.get("stopLossPct"), bot.BOT_STATE.get("stopLossPct", 0.8)), bot.numeric(query.get("takeProfitPct"), bot.BOT_STATE.get("takeProfitPct", 1.6)))
            await _send_json(send, 200, payload)
            return

        if method == "POST":
            payload = await _read_json(receive)
            if path == "/api/bot/start":
                await _send_json(send, 200, await asyncio.to_thread(_start_bot, payload))
                return
            if path == "/api/bot/stop":
                await _send_json(send, 200, _stop_bot())
                return
            if path == "/api/bot/manage-positions":
                await _send_json(send, 200, await asyncio.to_thread(_manage_positions))
                return
            if path == "/api/bybit/demo-order":
                if payload.get("confirmDemoOrder") is not True:
                    await _send_json(send, 400, {"ok": False, "error": "confirmDemoOrder must be true"})
                    return
                result = bot.place_demo_order(str(payload.get("symbol", "BTCUSDT")).upper(), "Sell" if payload.get("side") == "Sell" else "Buy", str(payload.get("qty", "0.001")), "manual", float(payload.get("stopLossPct", 0.8)), float(payload.get("takeProfitPct", 1.6)))
                bot.get_bot_engine().journal.add("manual_order", {"symbol": str(payload.get("symbol", "BTCUSDT")).upper(), "side": payload.get("side", "Buy"), "result": result})
                await _send_json(send, 200, result)
                return
            if path == "/api/bybit/kill-switch":
                symbol = str(payload.get("symbol", "BTCUSDT")).upper()
                with bot.BOT_LOCK:
                    bot.BOT_STATE.update({"enabled": False, "lastReason": "Auto trader stopped by kill switch."})
                cancel_result = bot.bybit_request("POST", "/v5/order/cancel-all", {"category": "linear", "symbol": symbol})
                close_result = bot.close_symbol_positions(symbol)
                bot.get_bot_engine().journal.add("kill_switch", {"symbol": symbol, "cancelResult": cancel_result, "closeResult": close_result})
                await _send_json(send, 200, {"retCode": 0, "retMsg": "Kill switch sent: open orders cancelled and positions close attempted", "cancelResult": cancel_result, "closeResult": close_result})
                return
        await _send_json(send, 404, {"ok": False, "error": "Route not found"})
    except Exception as exc:
        await _send_json(send, 500, {"ok": False, "error": str(exc)})
