from .indicators import avg_volume, candle_direction, ema, near_value, rsi, swing_zone, trend_direction


def vote(engine, signal, reason, strength=0):
    return {
        "engine": engine,
        "signal": signal,
        "reason": reason,
        "strength": round(float(strength), 2),
    }


def trend_following_engine(tf1h, tf15m, tf5m):
    direction = trend_direction(tf1h)
    if direction == "WAIT":
        return vote("Trend Follow", "WAIT", "1H EMA20/50 trend not clean")
    closes15 = [item["close"] for item in tf15m]
    ema20_15 = ema(closes15, 20)
    if not ema20_15:
        return vote("Trend Follow", "WAIT", "15M EMA setup unavailable")
    pullback = near_value(tf15m[-1]["close"], ema20_15[-1], 0.25)
    entry = candle_direction(tf5m[-1])
    volume_ok = tf5m[-1]["volume"] >= avg_volume(tf5m, 20) * 0.85
    if pullback and entry == direction and volume_ok:
        return vote("Trend Follow", direction, f"1H trend {direction}, 15M EMA pullback, 5M candle confirms", 2)
    return vote("Trend Follow", "WAIT", f"1H {direction}; waiting for 15M pullback + 5M close")


def sr_breakout_engine(tf1h, tf15m, tf5m):
    support, resistance = swing_zone(tf1h, 30)
    last5 = tf5m[-1]
    range15 = max(item["high"] for item in tf15m[-8:]) - min(item["low"] for item in tf15m[-8:])
    avg_range15 = sum(item["high"] - item["low"] for item in tf15m[-30:]) / 30
    consolidating = range15 <= avg_range15 * 4
    volume_ok = last5["volume"] >= avg_volume(tf5m, 20) * 1.15
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
    price_now = closes15[-1]
    price_prev = closes15[-8]
    rsi_now = rsi(closes15, 14)
    rsi_prev = rsi(closes15[:-8], 14)
    entry = candle_direction(tf5m[-1])
    bearish = price_now > price_prev and rsi_now < rsi_prev and entry == "Sell"
    bullish = price_now < price_prev and rsi_now > rsi_prev and entry == "Buy"
    if bearish and direction != "Buy":
        return vote("RSI Divergence", "Sell", f"15M bearish divergence, 5M reversal candle, RSI {rsi_now:.1f}", abs(rsi_now - rsi_prev))
    if bullish and direction != "Sell":
        return vote("RSI Divergence", "Buy", f"15M bullish divergence, 5M reversal candle, RSI {rsi_now:.1f}", abs(rsi_now - rsi_prev))
    return vote("RSI Divergence", "WAIT", f"No usable divergence, RSI {rsi_now:.1f}")


def vwap_bounce_engine(tf1h, tf15m, tf5m):
    direction = trend_direction(tf1h)
    if direction == "WAIT":
        return vote("VWAP Bounce", "WAIT", "1H trend not clean for VWAP bounce")
    total_volume = sum(item["volume"] for item in tf15m[-40:])
    if total_volume <= 0:
        return vote("VWAP Bounce", "WAIT", "15M VWAP volume unavailable")
    vwap = sum(((item["high"] + item["low"] + item["close"]) / 3) * item["volume"] for item in tf15m[-40:]) / total_volume
    near_vwap = near_value(tf15m[-1]["close"], vwap, 0.2)
    entry = candle_direction(tf5m[-1])
    volume_ok = tf5m[-1]["volume"] >= avg_volume(tf5m, 20) * 0.9
    if near_vwap and entry == direction and volume_ok:
        return vote("VWAP Bounce", direction, f"15M near VWAP, 5M {direction} bounce/rejection", abs(tf15m[-1]["close"] - vwap))
    return vote("VWAP Bounce", "WAIT", f"1H {direction}; waiting for VWAP bounce confirmation")


def orb_engine(tf1h, tf15m, tf5m):
    opening = tf1h[-2] if len(tf1h) >= 2 else tf1h[-1]
    high = opening["high"]
    low = opening["low"]
    last15 = tf15m[-1]
    last5 = tf5m[-1]
    volume_ok = last5["volume"] >= avg_volume(tf5m, 20) * 1.1
    if last15["close"] > high and last5["close"] > high and volume_ok:
        return vote("ORB", "Buy", "1H opening range high broken, 15M/5M confirmed", last5["close"] - high)
    if last15["close"] < low and last5["close"] < low and volume_ok:
        return vote("ORB", "Sell", "1H opening range low broken, 15M/5M confirmed", low - last5["close"])
    return vote("ORB", "WAIT", "Opening range not confirmed on 15M and 5M")
