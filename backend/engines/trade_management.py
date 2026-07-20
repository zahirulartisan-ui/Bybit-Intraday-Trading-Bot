import time
from decimal import Decimal, ROUND_HALF_UP


class TradeManagementEngine:
    def __init__(self, bybit_request_fn, market_data):
        self.bybit_request = bybit_request_fn
        self.market_data = market_data

    def tick_size(self, symbol):
        payload = self.market_data.public_get("/v5/market/instruments-info", {"category": "linear", "symbol": symbol})
        row = ((payload.get("result") or {}).get("list") or [{}])[0]
        tick = ((row.get("priceFilter") or {}).get("tickSize")) or "0.01"
        try:
            return Decimal(str(tick))
        except Exception:
            return Decimal("0.01")

    def mark_price(self, symbol):
        payload = self.market_data.public_get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
        row = ((payload.get("result") or {}).get("list") or [{}])[0]
        value = row.get("markPrice") or row.get("lastPrice")
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def format_price(self, symbol, value):
        tick = self.tick_size(symbol)
        price = Decimal(str(value))
        rounded = (price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
        return format(rounded.normalize(), "f")

    def tpsl_prices(self, symbol, side, stop_loss_pct, take_profit_pct):
        mark = self.mark_price(symbol)
        if not mark:
            return None, None
        if side == "Buy":
            stop_loss = mark * (1 - (float(stop_loss_pct) / 100))
            take_profit = mark * (1 + (float(take_profit_pct) / 100))
        else:
            stop_loss = mark * (1 + (float(stop_loss_pct) / 100))
            take_profit = mark * (1 - (float(take_profit_pct) / 100))
        return self.format_price(symbol, stop_loss), self.format_price(symbol, take_profit)

    def place_order(self, symbol, side, qty, source, stop_loss_pct=None, take_profit_pct=None):
        order = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": qty,
            "timeInForce": "IOC",
            "orderLinkId": f"codex-{source}-{int(time.time())}",
        }
        if stop_loss_pct is not None and take_profit_pct is not None:
            stop_loss, take_profit = self.tpsl_prices(symbol, side, stop_loss_pct, take_profit_pct)
            if stop_loss and take_profit:
                order.update({
                    "stopLoss": stop_loss,
                    "takeProfit": take_profit,
                    "tpslMode": "Full",
                    "tpOrderType": "Market",
                    "slOrderType": "Market",
                })
        return self.bybit_request("POST", "/v5/order/create", order)

    def close_positions(self, symbol):
        payload = self.bybit_request("GET", "/v5/position/list", {"category": "linear", "symbol": symbol})
        if payload.get("retCode") != 0:
            return {"ok": False, "error": payload.get("retMsg", "Position check failed"), "orders": []}
        orders = []
        for position in (payload.get("result") or {}).get("list") or []:
            size = abs(float(position.get("size") or 0))
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
                "orderLinkId": f"codex-close-{int(time.time())}",
            }
            if position.get("positionIdx") is not None:
                close_order["positionIdx"] = int(position.get("positionIdx") or 0)
            orders.append(self.bybit_request("POST", "/v5/order/create", close_order))
        return {"ok": True, "orders": orders}
