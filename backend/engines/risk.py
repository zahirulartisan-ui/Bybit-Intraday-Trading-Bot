import time


class RiskEngine:
    def __init__(self, position_size_fn, open_positions_count_fn=None):
        self.position_size_fn = position_size_fn
        self.open_positions_count_fn = open_positions_count_fn

    def check(self, state, signal):
        now = time.time()
        if signal not in ("Buy", "Sell"):
            return False, "No executable signal"
        if now - float(state.get("lastTradeAt") or 0) < int(state["cooldownSeconds"]):
            return False, "Cooldown active"
        position_size, position_msg = self.position_size_fn(state["symbol"])
        if position_size is None:
            return False, position_msg
        if position_size > 0:
            return False, "Position already open"
        if self.open_positions_count_fn:
            open_count, open_msg = self.open_positions_count_fn()
            if open_count is None:
                return False, open_msg
            max_open = max(1, int(state.get("maxOpenPositions") or 1))
            if open_count >= max_open:
                return False, f"Max open positions reached ({open_count}/{max_open})"
        return True, "Risk approved"
