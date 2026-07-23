import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engines.bot_engine import BotEngineV2 as ModularBotEngineV2


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
FRONTEND_INDEX = PROJECT_ROOT / "frontend" / "index.html"
ENV_PATH = ROOT / ".env"
RECV_WINDOW = "20000"
TOP_GAINER_REFRESH_SECONDS = 600
MIN_TURNOVER_24H = 750000
MAX_SPREAD_PCT = 0.25
MAX_TOP_GAINER_CHANGE_PCT = 45
MIN_TOP_GAINER_CHANGE_PCT = 1.2
MIN_LAST_PRICE = 0.01
MIN_VOLUME_24H_UNITS = 100000
BOT_SCAN_SECONDS = 30
DEFAULT_SCAN_SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "LTCUSDT",
]
ROUTER_MODES = {"conservative", "balanced", "aggressive"}
MARKET_UNIVERSE = {
    "symbols": list(DEFAULT_SCAN_SYMBOLS),
    "rows": [],
    "updatedAt": 0,
    "nextRefreshAt": 0,
    "source": "fallback",
}

BOT_STATE = {
    "enabled": False,
    "symbol": "BTCUSDT",
    "interval": "5",
    "qty": "0.001",
    "maxAllocationUsdt": 250,
    "riskPerTradePct": 0.5,
    "maxOpenPositions": 2,
    "dailyLossCapUsdt": 25,
    "maxTradesPerDay": 12,
    "breakevenEnabled": True,
    "breakevenTriggerPct": 0.6,
    "partialTpEnabled": True,
    "partialTpTriggerPct": 1.0,
    "partialTpClosePct": 50,
    "trailingStopEnabled": True,
    "trailingStopTriggerPct": 0.8,
    "trailingStopDistancePct": 0.35,
    "stopLossPct": 0.8,
    "takeProfitPct": 1.6,
    "cooldownSeconds": 120,
    "lastTradeAt": 0,
    "lastSignal": "WAIT",
    "lastReason": "Auto trader is stopped.",
    "engineVotes": [],
    "mode": "balanced",
    "autoPick": True,
    "scanSymbols": list(DEFAULT_SCAN_SYMBOLS),
    "symbolSource": "top_gainers",
    "selectedSignalSymbol": "BTCUSDT",
    "router": {
        "decision": "WAIT",
        "confidence": 0,
        "requiredVotes": 1,
        "mode": "balanced",
    },
    "lastOrder": None,
    "executionGuard": {"ok": True, "reason": "No execution attempted yet"},
    "orderLifecycle": {
        "signal": "WAIT",
        "guard": "idle",
        "order": "idle",
        "protection": "idle",
        "status": "idle",
        "reason": "No execution attempted yet",
    },
    "lastRunAt": None,
    "engineStatus": {},
    "scannerRows": [],
    "positionSizing": {},
    "tradeManagement": {},
}
BOT_LOCK = threading.Lock()
BOT_THREAD = None


class BotEngineV2:
    def __init__(self):
        self.version = "2.0.0"
        self.started_at = time.time()
        self.journal = []
        self.status = {
            "marketData": "idle",
            "indicator": "idle",
            "strategy": "idle",
            "router": "idle",
            "risk": "idle",
            "tradeManagement": "idle",
            "journal": "idle",
        }

    def set_status(self, engine, state):
        self.status[engine] = state

    def add_journal(self, event, payload=None):
        entry = {
            "time": int(time.time()),
            "event": event,
            "payload": payload or {},
        }
        self.journal.append(entry)
        self.journal = self.journal[-200:]
        self.set_status("journal", "ok")
        return entry

    def market_snapshot(self, symbol):
        self.set_status("marketData", "running")
        tf1h, message1h = fetch_candles(symbol, "60")
        tf15m, message15m = fetch_candles(symbol, "15")
        tf5m, message5m = fetch_candles(symbol, "5")
        ok = bool(tf1h and tf15m and tf5m)
        self.set_status("marketData", "ok" if ok else "error")
        return {
            "ok": ok,
            "timeframes": {"1H": tf1h, "15M": tf15m, "5M": tf5m},
            "message": "; ".join([message1h, message15m, message5m]),
        }

    def indicators(self, snapshot):
        self.set_status("indicator", "running")
        tf = snapshot["timeframes"]
        closes_1h = [item["close"] for item in tf["1H"]]
        closes_15m = [item["close"] for item in tf["15M"]]
        closes_5m = [item["close"] for item in tf["5M"]]
        values = {
            "trendDirection1H": trend_direction(tf["1H"]),
            "rsi15M": rsi(closes_15m, 14),
            "rsi5M": rsi(closes_5m, 14),
            "ema20_1H": (ema(closes_1h, 20) or [None])[-1],
            "ema50_1H": (ema(closes_1h, 50) or [None])[-1],
            "avgVolume5M": avg_volume(tf["5M"], 20),
        }
        self.set_status("indicator", "ok")
        return values

    def strategies(self, snapshot):
        self.set_status("strategy", "running")
        tf = snapshot["timeframes"]
        votes = [
            trend_following_engine(tf["1H"], tf["15M"], tf["5M"]),
            sr_breakout_engine(tf["1H"], tf["15M"], tf["5M"]),
            rsi_divergence_engine(tf["1H"], tf["15M"], tf["5M"]),
            vwap_bounce_engine(tf["1H"], tf["15M"], tf["5M"]),
            orb_engine(tf["1H"], tf["15M"], tf["5M"]),
        ]
        self.set_status("strategy", "ok")
        return votes

    def route(self, votes, mode="balanced"):
        self.set_status("router", "running")
        router = route_votes(votes, mode)
        self.set_status("router", "ok")
        return router

    def risk_check(self, state, signal):
        self.set_status("risk", "running")
        now = time.time()
        if signal not in ("Buy", "Sell"):
            self.set_status("risk", "wait")
            return False, "No executable signal"
        if now - float(state.get("lastTradeAt") or 0) < int(state["cooldownSeconds"]):
            self.set_status("risk", "blocked")
            return False, "Cooldown active"
        position_size, position_msg = get_position_size(state["symbol"])
        if position_size is None:
            self.set_status("risk", "error")
            return False, position_msg
        if position_size > 0:
            self.set_status("risk", "blocked")
            return False, "Position already open"
        self.set_status("risk", "ok")
        return True, "Risk approved"

    def execute(self, state, signal):
        self.set_status("tradeManagement", "running")
        result = place_demo_order(
            state["symbol"],
            signal,
            state["qty"],
            "auto",
            state["stopLossPct"],
            state["takeProfitPct"],
        )
        self.set_status("tradeManagement", "ok" if result.get("retCode") == 0 else "error")
        self.add_journal("auto_order", {"symbol": state["symbol"], "signal": signal, "result": result})
        return result

    def evaluate(self, symbol, mode="balanced"):
        snapshot = self.market_snapshot(symbol)
        if not snapshot["ok"]:
            router = route_votes([], mode)
            return "WAIT", snapshot["message"], [], router, {}, dict(self.status)
        indicators = self.indicators(snapshot)
        votes = self.strategies(snapshot)
        router = self.route(votes, mode)
        return router["decision"], router["reason"], votes, router, indicators, dict(self.status)

    def overview(self):
        return {
            "version": self.version,
            "uptimeSeconds": int(time.time() - self.started_at),
            "status": dict(self.status),
            "journal": list(self.journal[-50:]),
        }


BOT_ENGINE = None


def load_env():
    if not ENV_PATH.exists():
        return
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


def config():
    return {
        "api_key": os.environ.get("BYBIT_API_KEY", ""),
        "api_secret": os.environ.get("BYBIT_API_SECRET", ""),
        "base_url": os.environ.get("BYBIT_BASE_URL", "https://api-demo.bybit.com").rstrip("/"),
    }


def json_response(handler, status, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length == 0:
        return {}
    body = handler.rfile.read(length).decode("utf-8")
    return json.loads(body)


def sign(secret, payload):
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def bybit_timestamp_ms():
    cfg = config()
    request = urllib.request.Request(
        cfg["base_url"] + "/v5/market/time",
        headers={"Content-Type": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return str(int(time.time() * 1000))

    if payload.get("time"):
        return str(int(payload["time"]))

    result = payload.get("result") or {}
    if result.get("timeSecond"):
        return str(int(result["timeSecond"]) * 1000)

    return str(int(time.time() * 1000))


def bybit_request(method, path, params=None):
    cfg = config()
    if not cfg["api_key"] or not cfg["api_secret"]:
        return {
            "retCode": -1,
            "retMsg": "Missing BYBIT_API_KEY or BYBIT_API_SECRET in .env",
            "result": {},
        }

    timestamp = bybit_timestamp_ms()
    params = params or {}
    headers = {
        "Content-Type": "application/json",
        "X-BAPI-API-KEY": cfg["api_key"],
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
    }

    if method == "GET":
        query = urllib.parse.urlencode(params)
        signature_payload = timestamp + cfg["api_key"] + RECV_WINDOW + query
        url = cfg["base_url"] + path + (f"?{query}" if query else "")
        data = None
    else:
        body = json.dumps(params, separators=(",", ":"))
        signature_payload = timestamp + cfg["api_key"] + RECV_WINDOW + body
        url = cfg["base_url"] + path
        data = body.encode("utf-8")

    headers["X-BAPI-SIGN"] = sign(cfg["api_secret"], signature_payload)
    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(error_body)
        except json.JSONDecodeError:
            return {"retCode": exc.code, "retMsg": error_body, "result": {}}
    except Exception as exc:
        return {"retCode": -2, "retMsg": str(exc), "result": {}}


def public_bybit_get(path, params=None):
    cfg = config()
    query = urllib.parse.urlencode(params or {})
    url = cfg["base_url"] + path + (f"?{query}" if query else "")
    request = urllib.request.Request(url, headers={"Content-Type": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"retCode": -2, "retMsg": str(exc), "result": {}}


def numeric(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def top_gainer_universe(force=False, limit=10):
    now = int(time.time())
    if (
        not force
        and MARKET_UNIVERSE["symbols"]
        and MARKET_UNIVERSE["nextRefreshAt"] > now
    ):
        return dict(MARKET_UNIVERSE)

    payload = public_bybit_get("/v5/market/tickers", {"category": "linear"})
    rows = []
    if payload.get("retCode") == 0:
        for item in (payload.get("result") or {}).get("list") or []:
            symbol = str(item.get("symbol") or "").upper()
            if not symbol.endswith("USDT"):
                continue
            last_price = numeric(item.get("lastPrice"))
            turnover = numeric(item.get("turnover24h"))
            volume_24h = numeric(item.get("volume24h"))
            change = numeric(item.get("price24hPcnt"))
            bid = numeric(item.get("bid1Price"))
            ask = numeric(item.get("ask1Price"))
            if last_price <= 0 or turnover <= 0:
                continue
            change_pct = change * 100
            spread_pct = ((ask - bid) / last_price) * 100 if ask > 0 and bid > 0 and ask >= bid else 0
            filters = []
            if last_price < MIN_LAST_PRICE:
                filters.append("too_cheap")
            if turnover < MIN_TURNOVER_24H:
                filters.append("low_turnover")
            if volume_24h < MIN_VOLUME_24H_UNITS:
                filters.append("low_units")
            if spread_pct > MAX_SPREAD_PCT:
                filters.append("wide_spread")
            if change_pct > MAX_TOP_GAINER_CHANGE_PCT:
                filters.append("overextended")
            if change_pct < MIN_TOP_GAINER_CHANGE_PCT:
                filters.append("weak_momentum")
            if filters:
                continue
            rows.append({
                "symbol": symbol,
                "changePct": change_pct,
                "turnover24h": turnover,
                "volume24h": volume_24h,
                "spreadPct": round(spread_pct, 4),
                "lastPrice": last_price,
            })

    rows.sort(key=lambda row: (row["changePct"], row["turnover24h"]), reverse=True)
    selected = rows[:limit]
    if selected:
        MARKET_UNIVERSE.update({
            "symbols": [row["symbol"] for row in selected],
            "rows": selected,
            "updatedAt": now,
            "nextRefreshAt": now + TOP_GAINER_REFRESH_SECONDS,
            "source": "top_gainers",
        })
    else:
        MARKET_UNIVERSE.update({
            "symbols": list(DEFAULT_SCAN_SYMBOLS),
            "rows": [],
            "updatedAt": now,
            "nextRefreshAt": now + 60,
            "source": "fallback",
        })
    return dict(MARKET_UNIVERSE)


def get_mark_price(symbol):
    payload = public_bybit_get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
    if payload.get("retCode") != 0:
        return None
    row = ((payload.get("result") or {}).get("list") or [{}])[0]
    value = row.get("markPrice") or row.get("lastPrice")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_tick_size(symbol):
    payload = public_bybit_get("/v5/market/instruments-info", {"category": "linear", "symbol": symbol})
    if payload.get("retCode") != 0:
        return Decimal("0.01")
    row = ((payload.get("result") or {}).get("list") or [{}])[0]
    tick = ((row.get("priceFilter") or {}).get("tickSize")) or "0.01"
    try:
        return Decimal(str(tick))
    except Exception:
        return Decimal("0.01")


def get_instrument_rules(symbol):
    payload = public_bybit_get("/v5/market/instruments-info", {"category": "linear", "symbol": symbol})
    if payload.get("retCode") != 0:
        return {
            "ok": False,
            "reason": payload.get("retMsg", "Instrument rules unavailable"),
            "qtyStep": Decimal("0.001"),
            "minOrderQty": Decimal("0.001"),
            "maxOrderQty": Decimal("0"),
            "minNotionalValue": Decimal("5"),
            "tickSize": Decimal("0.01"),
        }
    row = ((payload.get("result") or {}).get("list") or [{}])[0]
    lot = row.get("lotSizeFilter") or {}
    price_filter = row.get("priceFilter") or {}
    return {
        "ok": bool(row),
        "reason": "OK" if row else "Instrument not found",
        "qtyStep": Decimal(str(lot.get("qtyStep") or "0.001")),
        "minOrderQty": Decimal(str(lot.get("minOrderQty") or "0.001")),
        "maxOrderQty": Decimal(str(lot.get("maxOrderQty") or lot.get("maxMktOrderQty") or "0")),
        "minNotionalValue": Decimal(str(lot.get("minNotionalValue") or "5")),
        "tickSize": Decimal(str(price_filter.get("tickSize") or "0.01")),
    }


def format_qty(value):
    qty = Decimal(str(value))
    return format(qty.normalize(), "f")


def floor_to_step(value, step):
    qty = Decimal(str(value))
    step = Decimal(str(step))
    if step <= 0:
        return qty
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to_step(value, step):
    qty = Decimal(str(value))
    step = Decimal(str(step))
    if step <= 0:
        return qty
    floored = floor_to_step(qty, step)
    if floored >= qty:
        return floored
    return floored + step


def get_wallet_equity():
    payload = bybit_request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    if payload.get("retCode") != 0:
        return None, payload.get("retMsg", "Wallet check failed")
    account = ((payload.get("result") or {}).get("list") or [{}])[0]
    try:
        return float(account.get("totalEquity") or 0), "OK"
    except (TypeError, ValueError):
        return None, "Wallet equity unavailable"


def calculate_position_sizing(symbol, state):
    mark = get_mark_price(symbol)
    if not mark:
        return {"ok": False, "reason": "Mark price unavailable", "qty": "0"}
    equity, equity_msg = get_wallet_equity()
    if equity is None or equity <= 0:
        return {"ok": False, "reason": equity_msg, "qty": "0"}

    rules = get_instrument_rules(symbol)
    if not rules.get("ok"):
        return {"ok": False, "reason": rules.get("reason", "Instrument rules unavailable"), "qty": "0"}
    risk_pct = max(0.01, float(state.get("riskPerTradePct") or 0.5))
    stop_pct = max(0.1, float(state.get("stopLossPct") or 0.8))
    max_allocation = max(1.0, float(state.get("maxAllocationUsdt") or 250))
    risk_amount = equity * (risk_pct / 100)
    stop_distance = mark * (stop_pct / 100)
    qty_by_risk = Decimal(str(risk_amount / stop_distance))
    qty_by_allocation = Decimal(str(max_allocation / mark))
    raw_qty = min(qty_by_risk, qty_by_allocation)
    qty = floor_to_step(raw_qty, rules["qtyStep"])

    min_notional_qty = Decimal("0")
    if rules["minNotionalValue"] > 0:
        min_notional_qty = Decimal(str(rules["minNotionalValue"])) / Decimal(str(mark))
        min_notional_qty = ceil_to_step(min_notional_qty, rules["qtyStep"])

    min_qty = max(rules["minOrderQty"], min_notional_qty)
    max_qty = rules.get("maxOrderQty") or Decimal("0")
    if max_qty > 0:
        qty = min(qty, max_qty)
    if qty < min_qty:
        if min_qty * Decimal(str(mark)) <= Decimal(str(max_allocation)):
            qty = min_qty
        else:
            return {
                "ok": False,
                "reason": "Calculated qty below exchange minimum and max allocation is too small",
                "qty": "0",
                "equity": equity,
                "markPrice": mark,
                "minQty": format_qty(min_qty),
                "maxAllocationUsdt": max_allocation,
                "minNotionalValue": format_qty(rules["minNotionalValue"]),
            }

    notional = qty * Decimal(str(mark))
    if rules["minNotionalValue"] > 0 and notional < rules["minNotionalValue"]:
        return {
            "ok": False,
            "reason": "Final qty does not meet min notional",
            "qty": "0",
            "notional": format_qty(notional),
            "minNotionalValue": format_qty(rules["minNotionalValue"]),
        }

    return {
        "ok": True,
        "reason": "Position size approved",
        "qty": format_qty(qty),
        "notional": format_qty(notional),
        "equity": round(equity, 4),
        "markPrice": mark,
        "riskAmount": round(risk_amount, 4),
        "riskPerTradePct": risk_pct,
        "maxAllocationUsdt": max_allocation,
        "minQty": format_qty(min_qty),
        "maxQty": format_qty(max_qty) if max_qty > 0 else "unlimited",
        "minNotionalValue": format_qty(rules["minNotionalValue"]),
        "qtyStep": format_qty(rules["qtyStep"]),
    }


def format_price(symbol, value):
    tick = get_tick_size(symbol)
    price = Decimal(str(value))
    rounded = (price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    return format(rounded.normalize(), "f")


def ema(values, period):
    if len(values) < period:
        return []
    alpha = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for value in values[period:]:
        result.append((value * alpha) + (result[-1] * (1 - alpha)))
    return result


def rsi(values, period=14):
    if len(values) <= period:
        return 50
    gains = []
    losses = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def simple_atr(highs, lows, closes, period=14):
    if len(closes) <= period:
        return 0
    ranges = []
    for i in range(1, len(closes)):
        high_low = highs[i] - lows[i]
        high_close = abs(highs[i] - closes[i - 1])
        low_close = abs(lows[i] - closes[i - 1])
        ranges.append(max(high_low, high_close, low_close))
    return sum(ranges[-period:]) / period


def adx_proxy(highs, lows, closes, period=14):
    atr = simple_atr(highs, lows, closes, period)
    if not atr or not closes[-1]:
        return 0
    return min(60, (atr / closes[-1]) * 10000)


def fetch_candles(symbol, interval, limit=120):
    payload = public_bybit_get("/v5/market/kline", {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    })
    if payload.get("retCode") != 0:
        return None, payload.get("retMsg", "Kline fetch failed")

    raw = (payload.get("result") or {}).get("list") or []
    raw = sorted(raw, key=lambda item: int(item[0]))
    candles = []
    for item in raw:
        if len(item) < 6:
            continue
        candles.append({
            "time": int(item[0]),
            "open": float(item[1]),
            "high": float(item[2]),
            "low": float(item[3]),
            "close": float(item[4]),
            "volume": float(item[5]),
        })
    if len(candles) < 60:
        return None, "Not enough candles"
    return candles, "OK"


def vote(engine, signal, reason, strength=0):
    return {
        "engine": engine,
        "signal": signal,
        "reason": reason,
        "strength": round(float(strength), 2),
    }


def trend_direction(candles):
    closes = [item["close"] for item in candles]
    fast = ema(closes, 20)
    slow = ema(closes, 50)
    if len(fast) < 2 or len(slow) < 2:
        return "WAIT"
    if fast[-1] > slow[-1] and closes[-1] > fast[-1]:
        return "Buy"
    if fast[-1] < slow[-1] and closes[-1] < fast[-1]:
        return "Sell"
    return "WAIT"


def avg_volume(candles, period=20):
    window = candles[-period:]
    if not window:
        return 0
    return sum(item["volume"] for item in window) / len(window)


def candle_direction(candle):
    if candle["close"] > candle["open"]:
        return "Buy"
    if candle["close"] < candle["open"]:
        return "Sell"
    return "WAIT"


def near_value(price, target, pct):
    if not target:
        return False
    return abs((price - target) / target) * 100 <= pct


def swing_zone(candles, period=20):
    window = candles[-period:]
    return min(item["low"] for item in window), max(item["high"] for item in window)


def trend_following_engine(tf1h, tf15m, tf5m):
    direction = trend_direction(tf1h)
    if direction == "WAIT":
        return vote("Trend Follow", "WAIT", "1H EMA20/50 trend not clean")

    closes15 = [item["close"] for item in tf15m]
    ema20_15 = ema(closes15, 20)
    if not ema20_15:
        return vote("Trend Follow", "WAIT", "15M EMA setup unavailable")
    pullback = near_value(tf15m[-1]["close"], ema20_15[-1], 0.4)
    entry = candle_direction(tf5m[-1])
    volume_ok = tf5m[-1]["volume"] >= avg_volume(tf5m, 20) * 0.85

    if pullback and entry == direction and volume_ok:
        return vote("Trend Follow", direction, f"1H trend {direction}, 15M EMA pullback, 5M candle confirms", 2)
    return vote("Trend Follow", "WAIT", f"1H {direction}; waiting for 15M pullback + 5M close")


def sr_breakout_engine(tf1h, tf15m, tf5m):
    support, resistance = swing_zone(tf1h, 30)
    last15 = tf15m[-1]
    last5 = tf5m[-1]
    range15 = max(item["high"] for item in tf15m[-8:]) - min(item["low"] for item in tf15m[-8:])
    avg_range15 = sum(item["high"] - item["low"] for item in tf15m[-30:]) / 30
    consolidating = range15 <= avg_range15 * 4
    volume_ok = last5["volume"] >= avg_volume(tf5m, 20) * 1.05

    if consolidating and last5["close"] > resistance and volume_ok:
        return vote("S/R Breakout", "Buy", "1H resistance breakout, 15M tight range, 5M volume confirm", last5["close"] - resistance)
    if consolidating and last5["close"] < support and volume_ok:
        return vote("S/R Breakout", "Sell", "1H support breakdown, 15M tight range, 5M volume confirm", support - last5["close"])
    return vote("S/R Breakout", "WAIT", "No confirmed 1H support/resistance breakout")


def rsi_divergence_engine(tf1h, tf15m, tf5m):
    direction = trend_direction(tf1h)
    closes15 = [item["close"] for item in tf15m]
    if len(closes15) < 29:
        return vote("RSI Divergence", "WAIT", "Not enough RSI history")

    recent15 = tf15m[-3:]
    prior15 = tf15m[-10:-7]
    price_now = sum(item["close"] for item in recent15) / len(recent15)
    price_prev = sum(item["close"] for item in prior15) / len(prior15)
    rsi_now = rsi(closes15, 14)
    rsi_prev = rsi(closes15[:-8], 14)
    entry = candle_direction(tf5m[-1])
    rsi_delta = abs(rsi_now - rsi_prev)
    price_delta_pct = abs(((price_now - price_prev) / price_prev) * 100) if price_prev else 0
    volume_ok = tf5m[-1]["volume"] >= avg_volume(tf5m, 20) * 1.0
    body = abs(tf5m[-1]["close"] - tf5m[-1]["open"])
    candle_range = max(tf5m[-1]["high"] - tf5m[-1]["low"], 0.00000001)
    reversal_body_ok = (body / candle_range) >= 0.25
    prev_entry = candle_direction(tf5m[-2]) if len(tf5m) >= 2 else "WAIT"
    follow_through_ok = entry in ("Buy", "Sell") and (prev_entry == entry or prev_entry == "WAIT")

    bearish = (
        price_now > price_prev
        and price_delta_pct >= 0.35
        and rsi_now < rsi_prev
        and rsi_now >= 55
        and rsi_delta >= 4
        and entry == "Sell"
        and volume_ok
        and reversal_body_ok
        and follow_through_ok
    )
    bullish = (
        price_now < price_prev
        and price_delta_pct >= 0.35
        and rsi_now > rsi_prev
        and rsi_now <= 45
        and rsi_delta >= 4
        and entry == "Buy"
        and volume_ok
        and reversal_body_ok
        and follow_through_ok
    )
    if bearish and direction != "Buy":
        return vote("RSI Divergence", "Sell", f"15M bearish divergence confirmed, RSI {rsi_now:.1f}, delta {rsi_delta:.1f}", rsi_delta)
    if bullish and direction != "Sell":
        return vote("RSI Divergence", "Buy", f"15M bullish divergence confirmed, RSI {rsi_now:.1f}, delta {rsi_delta:.1f}", rsi_delta)
    return vote("RSI Divergence", "WAIT", f"No confirmed divergence, RSI {rsi_now:.1f}, delta {rsi_delta:.1f}, move {price_delta_pct:.2f}%")


def vwap_bounce_engine(tf1h, tf15m, tf5m):
    direction = trend_direction(tf1h)
    if direction == "WAIT":
        return vote("VWAP Bounce", "WAIT", "1H trend not clean for VWAP bounce")
    total_volume = sum(item["volume"] for item in tf15m[-40:])
    if total_volume <= 0:
        return vote("VWAP Bounce", "WAIT", "15M VWAP volume unavailable")
    vwap = sum(((item["high"] + item["low"] + item["close"]) / 3) * item["volume"] for item in tf15m[-40:]) / total_volume
    near_vwap = near_value(tf15m[-1]["close"], vwap, 0.3)
    entry = candle_direction(tf5m[-1])
    volume_ok = tf5m[-1]["volume"] >= avg_volume(tf5m, 20) * 0.85

    if near_vwap and entry == direction and volume_ok:
        return vote("VWAP Bounce", direction, f"15M near VWAP, 5M {direction} bounce/rejection", abs(tf15m[-1]["close"] - vwap))
    return vote("VWAP Bounce", "WAIT", f"1H {direction}; waiting for VWAP bounce confirmation")


def orb_engine(tf1h, tf15m, tf5m):
    opening = tf1h[-2] if len(tf1h) >= 2 else tf1h[-1]
    high = opening["high"]
    low = opening["low"]
    last15 = tf15m[-1]
    last5 = tf5m[-1]
    volume_ok = last5["volume"] >= avg_volume(tf5m, 20) * 1.0

    if last15["close"] > high and last5["close"] > high and volume_ok:
        return vote("ORB", "Buy", "1H opening range high broken, 15M/5M confirmed", last5["close"] - high)
    if last15["close"] < low and last5["close"] < low and volume_ok:
        return vote("ORB", "Sell", "1H opening range low broken, 15M/5M confirmed", low - last5["close"])
    return vote("ORB", "WAIT", "Opening range not confirmed on 15M and 5M")


def legacy_trend_engine(closes, highs, lows):
    fast = ema(closes, 9)
    mid = ema(closes, 21)
    slow = ema(closes, 50)
    current_rsi = rsi(closes, 14)
    trend_strength = adx_proxy(highs, lows, closes)
    if len(fast) < 3 or len(mid) < 3 or len(slow) < 3:
        return vote("Trend", "WAIT", "Not enough EMA values")

    bullish_stack = fast[-1] > mid[-1] > slow[-1]
    bearish_stack = fast[-1] < mid[-1] < slow[-1]
    if bullish_stack and current_rsi < 68 and trend_strength >= 6:
        return vote("Trend", "Buy", f"EMA stack bullish, RSI {current_rsi:.1f}", trend_strength)
    if bearish_stack and current_rsi > 32 and trend_strength >= 6:
        return vote("Trend", "Sell", f"EMA stack bearish, RSI {current_rsi:.1f}", trend_strength)
    return vote("Trend", "WAIT", f"No clean trend, RSI {current_rsi:.1f}", trend_strength)


def legacy_vwap_engine(candles):
    window = candles[-40:]
    total_volume = sum(item["volume"] for item in window)
    if total_volume <= 0:
        return vote("VWAP", "WAIT", "No volume")
    vwap = sum(((item["high"] + item["low"] + item["close"]) / 3) * item["volume"] for item in window) / total_volume
    last = candles[-1]
    prev = candles[-2]
    closes = [item["close"] for item in candles]
    current_rsi = rsi(closes, 14)
    distance = ((last["close"] - vwap) / vwap) * 100
    near_vwap = abs(distance) <= 0.18
    volume_avg = sum(item["volume"] for item in candles[-21:-1]) / 20
    volume_ok = last["volume"] >= volume_avg * 1.05

    if near_vwap and last["close"] > prev["close"] and current_rsi < 66 and volume_ok:
        return vote("VWAP", "Buy", f"Pullback reclaimed VWAP, RSI {current_rsi:.1f}", abs(distance))
    if near_vwap and last["close"] < prev["close"] and current_rsi > 34 and volume_ok:
        return vote("VWAP", "Sell", f"VWAP rejection, RSI {current_rsi:.1f}", abs(distance))
    return vote("VWAP", "WAIT", f"VWAP distance {distance:.2f}%, RSI {current_rsi:.1f}", abs(distance))


def legacy_breakout_engine(candles):
    if len(candles) < 35:
        return vote("Breakout", "WAIT", "Not enough candles")
    lookback = candles[-31:-1]
    last = candles[-1]
    high_break = max(item["high"] for item in lookback)
    low_break = min(item["low"] for item in lookback)
    volume_avg = sum(item["volume"] for item in lookback) / len(lookback)
    volume_spike = last["volume"] >= volume_avg * 1.25
    breakout_margin = max(last["close"] - high_break, low_break - last["close"], 0)

    if last["close"] > high_break and volume_spike:
        return vote("Breakout", "Buy", "Range high breakout with volume", breakout_margin)
    if last["close"] < low_break and volume_spike:
        return vote("Breakout", "Sell", "Range low breakdown with volume", breakout_margin)
    return vote("Breakout", "WAIT", "No confirmed breakout", breakout_margin)


def normalize_mode(mode):
    mode = str(mode or "balanced").lower()
    return mode if mode in ROUTER_MODES else "balanced"


def vote_strength(vote_item):
    try:
        return abs(float(vote_item.get("strength") or 0))
    except (TypeError, ValueError):
        return 0.0


def route_votes(votes, mode="balanced"):
    mode = normalize_mode(mode)
    buy_votes = [item for item in votes if item["signal"] == "Buy"]
    sell_votes = [item for item in votes if item["signal"] == "Sell"]
    required = 2 if mode == "conservative" else 1
    if mode == "aggressive":
        buy_score = len(buy_votes) + sum(vote_strength(item) for item in buy_votes) / 100
        sell_score = len(sell_votes) + sum(vote_strength(item) for item in sell_votes) / 100
        if buy_votes and buy_score > sell_score:
            leader = max(buy_votes, key=vote_strength)
            return {
                "decision": "Buy",
                "confidence": len(buy_votes),
                "requiredVotes": required,
                "mode": mode,
                "reason": f"Aggressive demo approved Buy from {leader['engine']}",
            }
        if sell_votes and sell_score > buy_score:
            leader = max(sell_votes, key=vote_strength)
            return {
                "decision": "Sell",
                "confidence": len(sell_votes),
                "requiredVotes": required,
                "mode": mode,
                "reason": f"Aggressive demo approved Sell from {leader['engine']}",
            }
    if len(buy_votes) >= required and not sell_votes:
        leader = max(buy_votes, key=vote_strength)
        return {
            "decision": "Buy",
            "confidence": len(buy_votes),
            "requiredVotes": required,
            "mode": mode,
            "reason": f"{mode.title()} router approved Buy from {leader['engine']}",
        }
    if len(sell_votes) >= required and not buy_votes:
        leader = max(sell_votes, key=vote_strength)
        return {
            "decision": "Sell",
            "confidence": len(sell_votes),
            "requiredVotes": required,
            "mode": mode,
            "reason": f"{mode.title()} router approved Sell from {leader['engine']}",
        }
    if buy_votes and sell_votes:
        reason = "Router waiting because Buy/Sell engines conflict"
    elif mode == "conservative":
        reason = "Router waiting for 2 matching engine votes"
    else:
        reason = "Router waiting for at least 1 actionable engine vote"
    return {
        "decision": "WAIT",
        "confidence": max(len(buy_votes), len(sell_votes)),
        "requiredVotes": required,
        "mode": mode,
        "reason": reason,
    }


def get_position_size(symbol):
    payload = bybit_request("GET", "/v5/position/list", {"category": "linear", "symbol": symbol})
    if payload.get("retCode") != 0:
        return None, payload.get("retMsg", "Position check failed")
    positions = (payload.get("result") or {}).get("list") or []
    total_size = 0.0
    for position in positions:
        try:
            total_size += abs(float(position.get("size") or 0))
        except (TypeError, ValueError):
            pass
    return total_size, "OK"


def get_open_positions_count():
    payload = bybit_request("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
    if payload.get("retCode") != 0:
        return None, payload.get("retMsg", "Open position check failed")
    count = 0
    for position in (payload.get("result") or {}).get("list") or []:
        try:
            if abs(float(position.get("size") or 0)) > 0:
                count += 1
        except (TypeError, ValueError):
            pass
    return count, "OK"


def local_day_start_epoch():
    now = time.localtime()
    return int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, now.tm_wday, now.tm_yday, now.tm_isdst)))


def count_today_accepted_auto_orders(engine):
    start = local_day_start_epoch()
    count = 0
    for entry in getattr(engine.journal, "entries", []):
        if int(entry.get("time") or 0) < start:
            continue
        if entry.get("event") != "auto_order":
            continue
        payload = entry.get("payload") or {}
        result = payload.get("result") or {}
        try:
            accepted = int(result.get("retCode")) == 0
        except (TypeError, ValueError):
            accepted = False
        if accepted:
            count += 1
    return count


def get_daily_closed_pnl():
    start_ms = local_day_start_epoch() * 1000
    payload = bybit_request("GET", "/v5/position/closed-pnl", {
        "category": "linear",
        "startTime": str(start_ms),
        "limit": "100",
    })
    if payload.get("retCode") != 0:
        return None, payload.get("retMsg", "Closed PnL check failed")
    pnl = 0.0
    for row in (payload.get("result") or {}).get("list") or []:
        try:
            pnl += float(row.get("closedPnl") or 0)
        except (TypeError, ValueError):
            pass
    return pnl, "OK"


def daily_risk_report(state):
    cap = max(0.0, float(state.get("dailyLossCapUsdt") or 0))
    max_trades = max(1, int(state.get("maxTradesPerDay") or 1))
    closed_pnl, pnl_msg = get_daily_closed_pnl()
    if closed_pnl is None:
        return {
            "ok": False,
            "blocked": True,
            "reason": pnl_msg,
            "dailyLossCapUsdt": cap,
            "maxTradesPerDay": max_trades,
        }
    engine = get_bot_engine()
    trades_today = count_today_accepted_auto_orders(engine)
    loss_used = abs(min(0.0, closed_pnl))
    blocked = (cap > 0 and loss_used >= cap) or trades_today >= max_trades
    if cap > 0 and loss_used >= cap:
        reason = f"Daily loss cap reached (${loss_used:.2f}/${cap:.2f})"
    elif trades_today >= max_trades:
        reason = f"Max trades/day reached ({trades_today}/{max_trades})"
    else:
        reason = "Daily risk OK"
    return {
        "ok": True,
        "blocked": blocked,
        "reason": reason,
        "closedPnl": round(closed_pnl, 4),
        "lossUsed": round(loss_used, 4),
        "dailyLossCapUsdt": cap,
        "tradesToday": trades_today,
        "maxTradesPerDay": max_trades,
        "dayStart": local_day_start_epoch(),
    }


def get_open_positions():
    payload = bybit_request("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
    if payload.get("retCode") != 0:
        return None, payload.get("retMsg", "Open position check failed")
    positions = []
    for position in (payload.get("result") or {}).get("list") or []:
        try:
            size = abs(float(position.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        if size > 0:
            positions.append(position)
    return positions, "OK"


def get_symbol_open_positions(symbol):
    payload = bybit_request("GET", "/v5/position/list", {"category": "linear", "symbol": symbol})
    if payload.get("retCode") != 0:
        return None, payload.get("retMsg", "Symbol position check failed")
    positions = []
    for position in (payload.get("result") or {}).get("list") or []:
        try:
            size = abs(float(position.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        if size > 0:
            positions.append(position)
    return positions, "OK"


def summarize_position(position):
    try:
        size = abs(float(position.get("size") or 0))
    except (TypeError, ValueError):
        size = 0
    return {
        "symbol": position.get("symbol", ""),
        "side": position.get("side", ""),
        "size": size,
        "avgPrice": position.get("avgPrice"),
        "markPrice": position.get("markPrice"),
        "stopLoss": position.get("stopLoss"),
        "takeProfit": position.get("takeProfit"),
        "trailingStop": position.get("trailingStop"),
        "positionIdx": position.get("positionIdx"),
    }


def order_lifecycle(signal="WAIT", guard="idle", order="idle", protection="idle", status="idle", reason=""):
    return {
        "signal": signal,
        "guard": guard,
        "order": order,
        "protection": protection,
        "status": status,
        "reason": reason,
    }


def existing_position_guard(symbol, signal, state):
    positions, msg = get_symbol_open_positions(symbol)
    if positions is None:
        return {
            "ok": False,
            "reason": msg,
            "positions": [],
            "sameDirection": False,
            "oppositeDirection": False,
        }

    signal_side = "Buy" if signal == "Buy" else "Sell"
    summaries = [summarize_position(position) for position in positions]
    same = [position for position in summaries if position["side"] == signal_side]
    opposite = [position for position in summaries if position["side"] and position["side"] != signal_side]
    if same:
        return {
            "ok": False,
            "reason": f"Existing {symbol} {signal_side} position detected; duplicate entry blocked",
            "positions": summaries,
            "sameDirection": True,
            "oppositeDirection": False,
        }
    if opposite:
        return {
            "ok": False,
            "reason": f"Existing {symbol} opposite position detected; reverse trade blocked",
            "positions": summaries,
            "sameDirection": False,
            "oppositeDirection": True,
        }

    open_count, open_msg = get_open_positions_count()
    if open_count is None:
        return {
            "ok": False,
            "reason": open_msg,
            "positions": summaries,
            "sameDirection": False,
            "oppositeDirection": False,
        }
    max_open = max(1, int(state.get("maxOpenPositions") or 1))
    if open_count >= max_open:
        all_positions_payload = bybit_request("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        active_symbols = []
        if all_positions_payload.get("retCode") == 0:
            for row in (all_positions_payload.get("result") or {}).get("list") or []:
                try:
                    size = abs(float(row.get("size") or 0))
                except (TypeError, ValueError):
                    size = 0
                if size > 0 and row.get("symbol"):
                    active_symbols.append(str(row.get("symbol")))
        symbol_text = ", ".join(active_symbols[:5]) if active_symbols else symbol
        return {
            "ok": False,
            "reason": f"Max open positions reached ({open_count}/{max_open}); active: {symbol_text}",
            "positions": summaries,
            "openPositions": open_count,
            "maxOpenPositions": max_open,
            "sameDirection": False,
            "oppositeDirection": False,
        }

    return {
        "ok": True,
        "reason": "No existing position conflict",
        "positions": summaries,
        "openPositions": open_count,
        "maxOpenPositions": max_open,
        "sameDirection": False,
        "oppositeDirection": False,
    }


def position_key(position):
    symbol = position.get("symbol", "")
    open_time = position.get("openTime") or position.get("createdTime") or position.get("updatedTime") or "0"
    side = position.get("side", "")
    return f"{symbol}:{side}:{open_time}"


def journal_has_position_event(event, key):
    for entry in getattr(get_bot_engine().journal, "entries", []):
        if entry.get("event") != event:
            continue
        payload = entry.get("payload") or {}
        result = payload.get("result") or {}
        try:
            accepted = int(result.get("retCode")) == 0
        except (TypeError, ValueError):
            accepted = False
        if payload.get("positionKey") == key and accepted:
            return True
    return False


def close_partial_position(position, close_pct):
    symbol = position.get("symbol")
    side = position.get("side")
    try:
        size = Decimal(str(abs(float(position.get("size") or 0))))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "Position size unavailable"}
    if not symbol or not side or size <= 0:
        return {"ok": False, "reason": "No closeable position"}

    rules = get_instrument_rules(symbol)
    if not rules.get("ok"):
        return {"ok": False, "reason": rules.get("reason", "Instrument rules unavailable")}
    close_ratio = Decimal(str(max(1, min(100, float(close_pct))) / 100))
    close_qty = floor_to_step(size * close_ratio, rules["qtyStep"])
    if close_qty < rules["minOrderQty"] and size >= rules["minOrderQty"]:
        close_qty = min(size, rules["minOrderQty"])
    if close_qty <= 0 or close_qty > size:
        return {"ok": False, "reason": "Partial close qty below exchange minimum"}
    mark_price = Decimal(str(get_mark_price(symbol) or "0"))
    close_notional = close_qty * mark_price
    if mark_price <= 0:
        return {"ok": False, "reason": "Partial close mark price unavailable"}
    if close_notional < rules["minNotionalValue"]:
        return {
            "ok": False,
            "reason": "Partial close below exchange min notional",
            "qty": format_qty(close_qty),
            "notional": format_qty(close_notional),
            "minNotionalValue": format_qty(rules["minNotionalValue"]),
        }

    close_side = "Sell" if side == "Buy" else "Buy"
    order = {
        "category": "linear",
        "symbol": symbol,
        "side": close_side,
        "orderType": "Market",
        "qty": format_qty(close_qty),
        "reduceOnly": True,
        "timeInForce": "IOC",
        "orderLinkId": generate_order_link_id("partial"),
    }
    if position.get("positionIdx") is not None:
        order["positionIdx"] = int(position.get("positionIdx") or 0)
    return bybit_request("POST", "/v5/order/create", order)


def set_trailing_stop(position, distance_pct):
    symbol = position.get("symbol")
    side = position.get("side")
    try:
        mark_price = float(position.get("markPrice") or 0)
    except (TypeError, ValueError):
        mark_price = 0
    if not symbol or not side or mark_price <= 0:
        return {"retCode": -1, "retMsg": "Trailing stop mark price unavailable"}

    distance = mark_price * (max(0.05, float(distance_pct)) / 100)
    if side == "Buy":
        active_price = mark_price * 0.999
    else:
        active_price = mark_price * 1.001
    body = {
        "category": "linear",
        "symbol": symbol,
        "tpslMode": "Full",
        "trailingStop": format_price(symbol, distance),
        "activePrice": format_price(symbol, active_price),
    }
    if position.get("positionIdx") is not None:
        body["positionIdx"] = int(position.get("positionIdx") or 0)
    return bybit_request("POST", "/v5/position/trading-stop", body)


def manage_open_positions(state):
    positions, msg = get_open_positions()
    if positions is None:
        return {"ok": False, "actions": [], "reason": msg}

    actions = []
    breakeven_trigger_pct = max(0.1, float(state.get("breakevenTriggerPct") or 0.6))
    partial_trigger_pct = max(0.1, float(state.get("partialTpTriggerPct") or 1.0))
    partial_close_pct = max(1, min(100, float(state.get("partialTpClosePct") or 50)))
    trailing_trigger_pct = max(0.1, float(state.get("trailingStopTriggerPct") or 0.8))
    trailing_distance_pct = max(0.05, float(state.get("trailingStopDistancePct") or 0.35))
    for position in positions:
        symbol = position.get("symbol")
        side = position.get("side")
        try:
            avg_price = float(position.get("avgPrice") or 0)
            mark_price = float(position.get("markPrice") or 0)
        except (TypeError, ValueError):
            continue
        if not symbol or avg_price <= 0 or mark_price <= 0:
            continue
        key = position_key(position)
        if side == "Buy":
            pnl_pct = ((mark_price - avg_price) / avg_price) * 100
            breakeven_price = avg_price * 1.0002
            already_safe = float(position.get("stopLoss") or 0) >= avg_price
        else:
            pnl_pct = ((avg_price - mark_price) / avg_price) * 100
            breakeven_price = avg_price * 0.9998
            stop_loss = float(position.get("stopLoss") or 0)
            already_safe = stop_loss > 0 and stop_loss <= avg_price

        if (
            state.get("partialTpEnabled", True)
            and pnl_pct >= partial_trigger_pct
            and not journal_has_position_event("partial_take_profit", key)
        ):
            result = close_partial_position(position, partial_close_pct)
            action = {
                "type": "partial_take_profit",
                "positionKey": key,
                "symbol": symbol,
                "side": side,
                "pnlPct": round(pnl_pct, 4),
                "closePct": partial_close_pct,
                "result": result,
            }
            actions.append(action)
            get_bot_engine().journal.add("partial_take_profit", action)
            get_bot_engine().set_status("journal", "ok")

        if state.get("breakevenEnabled", True) and pnl_pct >= breakeven_trigger_pct and not already_safe:
            body = {
                "category": "linear",
                "symbol": symbol,
                "tpslMode": "Full",
                "stopLoss": format_price(symbol, breakeven_price),
            }
            if position.get("positionIdx") is not None:
                body["positionIdx"] = int(position.get("positionIdx") or 0)
            result = bybit_request("POST", "/v5/position/trading-stop", body)
            action = {
                "type": "breakeven_stop",
                "positionKey": key,
                "symbol": symbol,
                "side": side,
                "pnlPct": round(pnl_pct, 4),
                "stopLoss": body["stopLoss"],
                "result": result,
            }
            actions.append(action)
            get_bot_engine().journal.add("breakeven_stop", action)
            get_bot_engine().set_status("journal", "ok")

        if (
            state.get("trailingStopEnabled", True)
            and pnl_pct >= trailing_trigger_pct
            and not journal_has_position_event("trailing_stop_enabled", key)
        ):
            result = set_trailing_stop(position, trailing_distance_pct)
            action = {
                "type": "trailing_stop_enabled",
                "positionKey": key,
                "symbol": symbol,
                "side": side,
                "pnlPct": round(pnl_pct, 4),
                "distancePct": trailing_distance_pct,
                "result": result,
            }
            actions.append(action)
            get_bot_engine().journal.add("trailing_stop_enabled", action)
            get_bot_engine().set_status("journal", "ok")
    return {"ok": True, "actions": actions, "reason": "Managed open positions"}


def get_bot_engine():
    global BOT_ENGINE
    if BOT_ENGINE is None:
        BOT_ENGINE = ModularBotEngineV2(config()["base_url"], bybit_request, get_position_size, get_open_positions_count)
    return BOT_ENGINE


def evaluate_signal(symbol, interval, mode="balanced"):
    signal, reason, votes, router, indicators, status = get_bot_engine().evaluate(symbol, mode)
    return signal, reason, votes, router, indicators, status


def signal_score(row):
    router = row.get("router") or {}
    signal = row.get("signal")
    if signal not in ("Buy", "Sell"):
        return -1
    matching_votes = [item for item in row.get("engineVotes", []) if item.get("signal") == signal]
    return (int(router.get("confidence") or 0) * 1000) + sum(vote_strength(item) for item in matching_votes)


def select_best_signal(symbols, interval, mode):
    rows = []
    for symbol in symbols:
        signal, reason, votes, router, indicators, engine_status = evaluate_signal(symbol, interval, mode)
        rows.append({
            "symbol": symbol,
            "signal": signal,
            "reason": reason,
            "engineVotes": votes,
            "router": router,
            "indicators": indicators,
            "engineStatus": engine_status,
            "score": signal_score({
                "signal": signal,
                "engineVotes": votes,
                "router": router,
            }),
        })
    executable = [row for row in rows if row["signal"] in ("Buy", "Sell")]
    if executable:
        return max(executable, key=lambda row: row["score"]), rows
    return (rows[0] if rows else None), rows


def candles_until(candles, end_time, limit):
    rows = [item for item in candles if item["time"] <= end_time]
    return rows[-limit:]


def estimate_trade_outcome(side, entry_price, future, stop_loss_pct, take_profit_pct):
    if not future:
        return "open", 0, entry_price
    if side == "Buy":
        stop = entry_price * (1 - stop_loss_pct / 100)
        target = entry_price * (1 + take_profit_pct / 100)
        for index, candle in enumerate(future, start=1):
            hit_stop = candle["low"] <= stop
            hit_target = candle["high"] >= target
            if hit_stop and hit_target:
                return "loss", index, stop
            if hit_target:
                return "win", index, target
            if hit_stop:
                return "loss", index, stop
    else:
        stop = entry_price * (1 + stop_loss_pct / 100)
        target = entry_price * (1 - take_profit_pct / 100)
        for index, candle in enumerate(future, start=1):
            hit_stop = candle["high"] >= stop
            hit_target = candle["low"] <= target
            if hit_stop and hit_target:
                return "loss", index, stop
            if hit_target:
                return "win", index, target
            if hit_stop:
                return "loss", index, stop
    last_price = future[-1]["close"]
    pnl_pct = ((last_price - entry_price) / entry_price) * 100 if side == "Buy" else ((entry_price - last_price) / entry_price) * 100
    return ("win" if pnl_pct > 0 else "loss" if pnl_pct < 0 else "flat"), len(future), last_price


def replay_strategy_quality(symbol, horizon="24h", mode="balanced", stop_loss_pct=0.8, take_profit_pct=1.6):
    horizon = str(horizon or "24h").lower()
    replay_interval = "15" if horizon == "7d" else "5"
    limit = 700 if horizon == "7d" else 320
    lookahead = 16 if replay_interval == "15" else 24
    step = 2 if replay_interval == "15" else 3

    tf5, msg5 = fetch_candles(symbol, replay_interval, limit=limit)
    tf15, msg15 = fetch_candles(symbol, "15", limit=700)
    tf1h, msg1h = fetch_candles(symbol, "60", limit=220)
    if not tf5 or not tf15 or not tf1h:
        return {"ok": False, "message": msg5 if not tf5 else msg15 if not tf15 else msg1h, "trades": []}

    votes_by_engine = {}
    signal_counts = {"Buy": 0, "Sell": 0, "WAIT": 0}
    trades = []
    cursor_block_until = 0
    start_index = 80
    for index in range(start_index, max(start_index, len(tf5) - lookahead), step):
        if index <= cursor_block_until:
            continue
        end_time = tf5[index]["time"]
        replay5 = tf5[:index + 1]
        replay15 = candles_until(tf15, end_time, 120)
        replay1h = candles_until(tf1h, end_time, 120)
        if len(replay5) < 60 or len(replay15) < 60 or len(replay1h) < 60:
            continue

        votes = [
            trend_following_engine(replay1h, replay15, replay5),
            sr_breakout_engine(replay1h, replay15, replay5),
            rsi_divergence_engine(replay1h, replay15, replay5),
            vwap_bounce_engine(replay1h, replay15, replay5),
            orb_engine(replay1h, replay15, replay5),
        ]
        for item in votes:
            engine = item["engine"]
            votes_by_engine.setdefault(engine, {"Buy": 0, "Sell": 0, "WAIT": 0})
            votes_by_engine[engine][item["signal"]] = votes_by_engine[engine].get(item["signal"], 0) + 1

        router = route_votes(votes, mode)
        signal = router["decision"]
        signal_counts[signal] = signal_counts.get(signal, 0) + 1
        if signal not in ("Buy", "Sell"):
            continue

        entry = replay5[-1]["close"]
        outcome, bars_held, exit_price = estimate_trade_outcome(signal, entry, tf5[index + 1:index + 1 + lookahead], stop_loss_pct, take_profit_pct)
        pnl_pct = ((exit_price - entry) / entry) * 100 if signal == "Buy" else ((entry - exit_price) / entry) * 100
        trades.append({
            "time": end_time,
            "symbol": symbol,
            "side": signal,
            "entry": round(entry, 8),
            "exit": round(exit_price, 8),
            "outcome": outcome,
            "pnlPct": round(pnl_pct, 4),
            "barsHeld": bars_held,
            "router": router,
            "votes": votes,
        })
        cursor_block_until = index + max(1, bars_held)

    wins = len([item for item in trades if item["outcome"] == "win"])
    losses = len([item for item in trades if item["outcome"] == "loss"])
    flats = len(trades) - wins - losses
    total_pnl = sum(item["pnlPct"] for item in trades)
    return {
        "ok": True,
        "symbol": symbol,
        "horizon": horizon,
        "interval": replay_interval,
        "mode": normalize_mode(mode),
        "candles": len(tf5),
        "stopLossPct": stop_loss_pct,
        "takeProfitPct": take_profit_pct,
        "summary": {
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "flats": flats,
            "winRate": round((wins / len(trades)) * 100, 2) if trades else 0,
            "estimatedPnlPct": round(total_pnl, 4),
        },
        "signalCounts": signal_counts,
        "votesByEngine": votes_by_engine,
        "trades": trades[-100:],
    }


def tpsl_prices(symbol, side, stop_loss_pct, take_profit_pct):
    mark = get_mark_price(symbol)
    if not mark:
        return None, None
    stop_loss_pct = max(0, float(stop_loss_pct or 0))
    take_profit_pct = max(0, float(take_profit_pct or 0))
    if side == "Buy":
        stop_loss = mark * (1 - (stop_loss_pct / 100))
        take_profit = mark * (1 + (take_profit_pct / 100))
    else:
        stop_loss = mark * (1 + (stop_loss_pct / 100))
        take_profit = mark * (1 - (take_profit_pct / 100))
    return format_price(symbol, stop_loss), format_price(symbol, take_profit)


def generate_order_link_id(source):
    prefix = "".join(ch.lower() for ch in str(source or "auto") if ch.isalnum())[:8] or "auto"
    nonce = secrets.token_hex(3)
    return f"cdx-{prefix}-{int(time.time() * 1000)}-{nonce}"[:36]


def place_demo_order(symbol, side, qty, source, stop_loss_pct=None, take_profit_pct=None):
    order = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": qty,
        "timeInForce": "IOC",
        "orderLinkId": generate_order_link_id(source),
    }

    if stop_loss_pct is not None and take_profit_pct is not None:
        if get_mark_price(symbol) is None:
            return {"retCode": -1, "retMsg": "Could not fetch mark price for TP/SL"}
        stop_loss, take_profit = tpsl_prices(symbol, side, stop_loss_pct, take_profit_pct)
        if stop_loss and take_profit:
            order.update({
                "stopLoss": stop_loss,
                "takeProfit": take_profit,
                "tpslMode": "Full",
                "tpOrderType": "Market",
                "slOrderType": "Market",
            })

    return bybit_request("POST", "/v5/order/create", order)


def fetch_order_status(symbol, order_result):
    result = order_result.get("result") or {}
    order_id = result.get("orderId")
    order_link_id = result.get("orderLinkId")
    if not order_id and not order_link_id:
        return {"ok": False, "reason": "Order id unavailable"}

    params = {"category": "linear", "symbol": symbol}
    if order_id:
        params["orderId"] = order_id
    elif order_link_id:
        params["orderLinkId"] = order_link_id

    realtime = bybit_request("GET", "/v5/order/realtime", params)
    rows = (realtime.get("result") or {}).get("list") or []
    if realtime.get("retCode") == 0 and rows:
        row = rows[0]
        return {
            "ok": True,
            "source": "realtime",
            "orderId": row.get("orderId") or order_id,
            "orderLinkId": row.get("orderLinkId") or order_link_id,
            "orderStatus": row.get("orderStatus") or "Unknown",
            "cumExecQty": row.get("cumExecQty"),
            "avgPrice": row.get("avgPrice"),
        }

    history = bybit_request("GET", "/v5/order/history", params)
    rows = (history.get("result") or {}).get("list") or []
    if history.get("retCode") == 0 and rows:
        row = rows[0]
        return {
            "ok": True,
            "source": "history",
            "orderId": row.get("orderId") or order_id,
            "orderLinkId": row.get("orderLinkId") or order_link_id,
            "orderStatus": row.get("orderStatus") or "Unknown",
            "cumExecQty": row.get("cumExecQty"),
            "avgPrice": row.get("avgPrice"),
        }

    return {
        "ok": False,
        "reason": realtime.get("retMsg") or history.get("retMsg") or "Order status unavailable",
        "orderId": order_id,
        "orderLinkId": order_link_id,
    }


def close_symbol_positions(symbol):
    payload = bybit_request("GET", "/v5/position/list", {"category": "linear", "symbol": symbol})
    if payload.get("retCode") != 0:
        return {"ok": False, "error": payload.get("retMsg", "Position check failed"), "orders": []}

    orders = []
    positions = (payload.get("result") or {}).get("list") or []
    for position in positions:
        try:
            size = abs(float(position.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            continue
        side = "Sell" if position.get("side") == "Buy" else "Buy"
        close_order = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(position.get("size")),
            "reduceOnly": True,
            "timeInForce": "IOC",
            "orderLinkId": generate_order_link_id("close"),
        }
        if position.get("positionIdx") is not None:
            close_order["positionIdx"] = int(position.get("positionIdx") or 0)
        orders.append(bybit_request("POST", "/v5/order/create", close_order))
    return {"ok": True, "orders": orders}


def bot_tick():
    with BOT_LOCK:
        state = dict(BOT_STATE)

    symbol = state["symbol"]
    interval = state["interval"]
    mode = normalize_mode(state.get("mode"))
    auto_pick = bool(state.get("autoPick"))
    universe = top_gainer_universe()
    scan_symbols = list(universe["symbols"])
    now = time.time()
    management = manage_open_positions(state)
    daily_risk = daily_risk_report(state)

    if auto_pick:
        best, scan_rows = select_best_signal(scan_symbols, interval, mode)
        if best:
            symbol = best["symbol"]
            signal = best["signal"]
            reason = best["reason"]
            votes = best["engineVotes"]
            router = best["router"]
            indicators = best["indicators"]
            engine_status = best["engineStatus"]
        else:
            signal, reason, votes, router, indicators, engine_status = evaluate_signal(symbol, interval, mode)
            scan_rows = []
    else:
        signal, reason, votes, router, indicators, engine_status = evaluate_signal(symbol, interval, mode)
        scan_rows = []

    active_state = dict(state)
    active_state["symbol"] = symbol
    sizing = calculate_position_sizing(symbol, active_state) if signal in ("Buy", "Sell") else {}
    if sizing.get("ok"):
        active_state["qty"] = sizing["qty"]
    update = {
        "lastRunAt": int(now),
        "lastSignal": signal,
        "lastReason": reason,
        "engineVotes": votes,
        "router": router,
        "indicators": indicators,
        "engineStatus": engine_status,
        "selectedSignalSymbol": symbol,
        "scannerRows": scan_rows,
        "scanSymbols": scan_symbols,
        "symbolSource": universe["source"],
        "universe": universe,
        "mode": mode,
        "positionSizing": sizing,
        "tradeManagement": management,
        "dailyRisk": daily_risk,
        "executionGuard": {"ok": True, "reason": "No execution signal"},
        "orderLifecycle": order_lifecycle(signal=signal, reason=reason),
    }

    if signal in ("Buy", "Sell"):
        if daily_risk.get("blocked"):
            update["lastReason"] = reason + f"; daily risk blocked: {daily_risk.get('reason', 'Daily risk blocked')}"
            update["executionGuard"] = {"ok": False, "reason": daily_risk.get("reason", "Daily risk blocked")}
            update["orderLifecycle"] = order_lifecycle(signal=signal, guard="blocked", order="skipped", protection="skipped", status="blocked", reason=update["lastReason"])
        elif not sizing.get("ok"):
            update["lastReason"] = reason + f"; sizing blocked: {sizing.get('reason', 'Unknown sizing error')}"
            update["executionGuard"] = {"ok": False, "reason": sizing.get("reason", "Unknown sizing error")}
            update["orderLifecycle"] = order_lifecycle(signal=signal, guard="blocked", order="skipped", protection="skipped", status="blocked", reason=update["lastReason"])
        else:
            engine = get_bot_engine()
            guard = existing_position_guard(symbol, signal, active_state)
            update["executionGuard"] = guard
            if not guard.get("ok"):
                engine.set_status("risk", "blocked")
                update["engineStatus"] = dict(engine.status)
                update["lastReason"] = reason + f"; execution guard blocked: {guard.get('reason', 'Position guard blocked')}"
                update["orderLifecycle"] = order_lifecycle(signal=signal, guard="blocked", order="skipped", protection="skipped", status="blocked", reason=guard.get("reason", "Position guard blocked"))
            else:
                approved, risk_reason = engine.risk_check(active_state, signal)
                update["engineStatus"] = dict(engine.status)
                if not approved:
                    update["lastReason"] = reason + f"; {risk_reason}"
                    update["executionGuard"] = {**guard, "ok": False, "reason": risk_reason}
                    update["orderLifecycle"] = order_lifecycle(signal=signal, guard="blocked", order="skipped", protection="skipped", status="blocked", reason=risk_reason)
                else:
                    result = engine.execute(active_state, signal)
                    update["engineStatus"] = dict(engine.status)
                    update["lastOrder"] = result
                    update["qty"] = active_state["qty"]
                    protection_status = "attached" if (
                        result.get("retCode") == 0
                        and active_state.get("stopLossPct") is not None
                        and active_state.get("takeProfitPct") is not None
                    ) else "skipped"
                    if result.get("retCode") == 0:
                        order_status = fetch_order_status(symbol, result)
                        result["lifecycleStatus"] = order_status
                        update["lastTradeAt"] = int(now)
                        update["lastReason"] = reason + f"; demo order accepted with qty {active_state['qty']}"
                        lifecycle_status = order_status.get("orderStatus") or "open-check-pending"
                        update["orderLifecycle"] = order_lifecycle(signal=signal, guard="passed", order=lifecycle_status, protection=protection_status, status="accepted", reason=update["lastReason"])
                    else:
                        update["lastReason"] = reason + f"; order rejected: {result.get('retMsg', 'Unknown error')}"
                        update["orderLifecycle"] = order_lifecycle(signal=signal, guard="passed", order="rejected", protection="skipped", status="rejected", reason=result.get("retMsg", "Unknown error"))

    with BOT_LOCK:
        BOT_STATE.update(update)
        return dict(BOT_STATE)


def bot_loop():
    while True:
        with BOT_LOCK:
            enabled = BOT_STATE["enabled"]
        if not enabled:
            break
        bot_tick()
        time.sleep(BOT_SCAN_SECONDS)


def ensure_bot_thread():
    global BOT_THREAD
    if BOT_THREAD and BOT_THREAD.is_alive():
        return
    BOT_THREAD = threading.Thread(target=bot_loop, daemon=True)
    BOT_THREAD.start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = dict(urllib.parse.parse_qsl(parsed.query))

        if parsed.path in ("/", "/index.html", "/app"):
            if FRONTEND_INDEX.exists():
                html = FRONTEND_INDEX.read_text(encoding="utf-8")
                html = html.replace('const apiParam = new URLSearchParams(window.location.search).get("api");', 'const apiParam = new URLSearchParams(window.location.search).get("api") || window.location.origin;')
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
            else:
                json_response(self, 404, {"ok": False, "error": "Frontend index not found"})
            return

        if parsed.path == "/api/health":
            cfg = config()
            json_response(self, 200, {
                "ok": True,
                "exchange": "bybit",
                "baseUrl": cfg["base_url"],
                "hasApiKey": bool(cfg["api_key"]),
                "hasApiSecret": bool(cfg["api_secret"]),
            })
            return

        if parsed.path == "/api/bybit/ticker":
            symbol = query.get("symbol", "BTCUSDT").upper()
            payload = public_bybit_get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/bybit/kline":
            symbol = query.get("symbol", "BTCUSDT").upper()
            interval = query.get("interval", "5")
            candles, message = fetch_candles(symbol, interval)
            json_response(self, 200, {
                "ok": bool(candles),
                "symbol": symbol,
                "interval": interval,
                "candles": candles or [],
                "message": message,
            })
            return

        if parsed.path == "/api/bybit/wallet":
            payload = bybit_request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/bot/sizing":
            symbol = query.get("symbol", "BTCUSDT").upper()
            with BOT_LOCK:
                state = dict(BOT_STATE)
            sizing = calculate_position_sizing(symbol, state)
            json_response(self, 200, {
                "ok": bool(sizing.get("ok")),
                "symbol": symbol,
                "sizing": sizing,
            })
            return

        if parsed.path == "/api/bybit/positions":
            symbol = query.get("symbol", "BTCUSDT").upper()
            payload = bybit_request("GET", "/v5/position/list", {"category": "linear", "symbol": symbol})
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/bybit/open-orders":
            symbol = query.get("symbol", "BTCUSDT").upper()
            payload = bybit_request("GET", "/v5/order/realtime", {"category": "linear", "symbol": symbol})
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/bot/status":
            with BOT_LOCK:
                payload = dict(BOT_STATE)
                payload["engineOverview"] = get_bot_engine().overview()
                payload["universe"] = top_gainer_universe()
                payload["dailyRisk"] = daily_risk_report(payload)
                payload["scanSeconds"] = BOT_SCAN_SECONDS
                payload["topGainerRefreshSeconds"] = TOP_GAINER_REFRESH_SECONDS
            json_response(self, 200, {"ok": True, "bot": payload})
            return

        if parsed.path == "/api/bot/universe":
            force = query.get("force", "0") in ("1", "true", "yes")
            json_response(self, 200, {"ok": True, "universe": top_gainer_universe(force=force)})
            return

        if parsed.path == "/api/bot/engine":
            json_response(self, 200, {"ok": True, "engine": get_bot_engine().overview()})
            return

        if parsed.path == "/api/bot/journal":
            limit = max(1, min(500, int(query.get("limit", "100"))))
            engine = get_bot_engine()
            json_response(self, 200, {
                "ok": True,
                "journal": engine.journal.recent(limit),
                "journalPath": str(engine.journal.path),
            })
            return

        if parsed.path == "/api/bot/scanner":
            universe = top_gainer_universe(force=query.get("forceUniverse", "0") in ("1", "true", "yes"))
            symbols = query.get("symbols", ",".join(universe["symbols"]))
            interval = query.get("interval", "15")
            mode = normalize_mode(query.get("mode", "balanced"))
            market_rows = {row.get("symbol"): row for row in universe.get("rows", [])}
            rows = []
            for symbol in [item.strip().upper() for item in symbols.split(",") if item.strip()]:
                signal, reason, votes, router, indicators, engine_status = evaluate_signal(symbol, interval, mode)
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
                    "score": signal_score({"signal": signal, "engineVotes": votes, "router": router}),
                })
            rows.sort(key=lambda row: row["score"], reverse=True)
            json_response(self, 200, {
                "ok": True,
                "interval": interval,
                "mode": mode,
                "rows": rows,
                "universe": universe,
                "scanSeconds": BOT_SCAN_SECONDS,
                "topGainerRefreshSeconds": TOP_GAINER_REFRESH_SECONDS,
            })
            return

        if parsed.path == "/api/bot/replay":
            symbol = query.get("symbol", "BTCUSDT").upper()
            horizon = query.get("horizon", "24h")
            mode = normalize_mode(query.get("mode", BOT_STATE.get("mode", "balanced")))
            stop_loss_pct = numeric(query.get("stopLossPct"), BOT_STATE.get("stopLossPct", 0.8))
            take_profit_pct = numeric(query.get("takeProfitPct"), BOT_STATE.get("takeProfitPct", 1.6))
            payload = replay_strategy_quality(symbol, horizon, mode, stop_loss_pct, take_profit_pct)
            json_response(self, 200, payload)
            return

        json_response(self, 404, {"ok": False, "error": "Route not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        try:
            payload = read_json(self)
        except Exception as exc:
            json_response(self, 400, {"ok": False, "error": f"Invalid JSON: {exc}"})
            return

        if parsed.path == "/api/bybit/demo-order":
            if payload.get("confirmDemoOrder") is not True:
                json_response(self, 400, {"ok": False, "error": "confirmDemoOrder must be true"})
                return

            symbol = str(payload.get("symbol", "BTCUSDT")).upper()
            side = "Sell" if payload.get("side") == "Sell" else "Buy"
            qty = str(payload.get("qty", "0.001"))
            stop_loss_pct = float(payload.get("stopLossPct", 0.8))
            take_profit_pct = float(payload.get("takeProfitPct", 1.6))
            result = place_demo_order(symbol, side, qty, "manual", stop_loss_pct, take_profit_pct)
            get_bot_engine().journal.add("manual_order", {"symbol": symbol, "side": side, "result": result})
            get_bot_engine().set_status("journal", "ok")
            json_response(self, 200, result)
            return

        if parsed.path == "/api/bot/start":
            universe = top_gainer_universe(force=True)
            symbol = str(payload.get("symbol") or universe["symbols"][0] or "BTCUSDT").upper()
            interval = str(payload.get("interval", "5"))
            qty = str(payload.get("qty", "0.001"))
            stop_loss_pct = float(payload.get("stopLossPct", 0.8))
            take_profit_pct = float(payload.get("takeProfitPct", 1.6))
            max_allocation = max(1, float(payload.get("maxAllocationUsdt", 250)))
            risk_per_trade = max(0.01, float(payload.get("riskPerTradePct", 0.5)))
            max_open_positions = max(1, int(payload.get("maxOpenPositions", 1)))
            daily_loss_cap = max(0, float(payload.get("dailyLossCapUsdt", 25)))
            max_trades_per_day = max(1, int(payload.get("maxTradesPerDay", 5)))
            breakeven_enabled = payload.get("breakevenEnabled", True) is not False
            breakeven_trigger = max(0.1, float(payload.get("breakevenTriggerPct", 0.6)))
            partial_tp_enabled = payload.get("partialTpEnabled", True) is not False
            partial_tp_trigger = max(0.1, float(payload.get("partialTpTriggerPct", 1.0)))
            partial_tp_close = max(1, min(100, float(payload.get("partialTpClosePct", 50))))
            trailing_stop_enabled = payload.get("trailingStopEnabled", True) is not False
            trailing_stop_trigger = max(0.1, float(payload.get("trailingStopTriggerPct", 0.8)))
            trailing_stop_distance = max(0.05, float(payload.get("trailingStopDistancePct", 0.35)))
            cooldown = max(60, int(payload.get("cooldownSeconds", 300)))
            mode = normalize_mode(payload.get("mode", "balanced"))
            auto_pick = True
            scan_symbols = list(universe["symbols"])
            with BOT_LOCK:
                BOT_STATE.update({
                    "enabled": True,
                    "symbol": symbol,
                    "interval": interval,
                    "qty": qty,
                    "maxAllocationUsdt": max_allocation,
                    "riskPerTradePct": risk_per_trade,
                    "maxOpenPositions": max_open_positions,
                    "dailyLossCapUsdt": daily_loss_cap,
                    "maxTradesPerDay": max_trades_per_day,
                    "breakevenEnabled": breakeven_enabled,
                    "breakevenTriggerPct": breakeven_trigger,
                    "partialTpEnabled": partial_tp_enabled,
                    "partialTpTriggerPct": partial_tp_trigger,
                    "partialTpClosePct": partial_tp_close,
                    "trailingStopEnabled": trailing_stop_enabled,
                    "trailingStopTriggerPct": trailing_stop_trigger,
                    "trailingStopDistancePct": trailing_stop_distance,
                    "stopLossPct": stop_loss_pct,
                    "takeProfitPct": take_profit_pct,
                    "cooldownSeconds": cooldown,
                    "mode": mode,
                    "autoPick": auto_pick,
                    "scanSymbols": scan_symbols,
                    "symbolSource": universe["source"],
                    "selectedSignalSymbol": symbol,
                    "universe": universe,
                    "lastReason": f"Auto trader started in {mode} mode with top-gainer scan.",
                })
            ensure_bot_thread()
            status = bot_tick()
            json_response(self, 200, {"ok": True, "bot": status})
            return

        if parsed.path == "/api/bot/stop":
            with BOT_LOCK:
                BOT_STATE.update({
                    "enabled": False,
                    "lastReason": "Auto trader stopped by user.",
                })
                status = dict(BOT_STATE)
            json_response(self, 200, {"ok": True, "bot": status})
            return

        if parsed.path == "/api/bot/manage-positions":
            with BOT_LOCK:
                state = dict(BOT_STATE)
            result = manage_open_positions(state)
            with BOT_LOCK:
                BOT_STATE.update({"tradeManagement": result})
                status = dict(BOT_STATE)
            json_response(self, 200, {"ok": result.get("ok", False), "result": result, "bot": status})
            return

        if parsed.path == "/api/bybit/kill-switch":
            symbol = str(payload.get("symbol", "BTCUSDT")).upper()
            with BOT_LOCK:
                BOT_STATE.update({
                    "enabled": False,
                    "lastReason": "Auto trader stopped by kill switch.",
                })
            cancel_result = bybit_request("POST", "/v5/order/cancel-all", {"category": "linear", "symbol": symbol})
            close_result = close_symbol_positions(symbol)
            get_bot_engine().journal.add("kill_switch", {"symbol": symbol, "cancelResult": cancel_result, "closeResult": close_result})
            get_bot_engine().set_status("journal", "ok")
            json_response(self, 200, {
                "retCode": 0 if cancel_result.get("retCode") == 0 and close_result.get("ok") else cancel_result.get("retCode", -1),
                "retMsg": "Kill switch sent: open orders cancelled and positions close attempted",
                "cancelResult": cancel_result,
                "closeResult": close_result,
            })
            return

        json_response(self, 404, {"ok": False, "error": "Route not found"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Bybit demo backend running on http://{host}:{port}", flush=True)
    print(f"Reading environment from {ENV_PATH}", flush=True)
    server.serve_forever()
