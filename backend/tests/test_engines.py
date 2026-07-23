import sys
import os
from datetime import datetime, timezone

# Add backend to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engines.market_data import MarketDataEngine
from engines.strategies import orb_engine as modular_orb_engine
from server import orb_engine as legacy_orb_engine, fetch_candles

class DummyResponse:
    def __init__(self, data):
        self.data = data
    def read(self):
        return self.data
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

def test_market_data_snapshot_handles_none():
    engine = MarketDataEngine("https://api-demo.bybit.com")

    # Mock self.candles to return None/strings instead of crashing
    original_candles = engine.candles
    try:
        # Scenario where one of them returns None for message
        engine.candles = lambda symbol, interval: ([], None) if interval == "5" else ([], "OK")
        res = engine.snapshot("BTCUSDT")
        assert res["ok"] is False
        assert res["message"] == "OK; OK"

        # Scenario where all return None for message
        engine.candles = lambda symbol, interval: ([], None)
        res = engine.snapshot("BTCUSDT")
        assert res["ok"] is False
        assert res["message"] == ""
    finally:
        engine.candles = original_candles

def test_orb_engine_no_candles_today():
    # Setup test data with candles entirely before today
    now = datetime.now(timezone.utc)
    today_utc_midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    today_utc_midnight_ms = int(today_utc_midnight.timestamp() * 1000)

    # 1H candles all in the past day
    tf1h = [
        {"time": today_utc_midnight_ms - 3600000 * 2, "open": 100, "high": 110, "low": 90, "close": 105, "volume": 10},
        {"time": today_utc_midnight_ms - 3600000, "open": 105, "high": 115, "low": 95, "close": 100, "volume": 10},
    ]
    tf15m = [{"time": today_utc_midnight_ms, "open": 100, "high": 110, "low": 90, "close": 105, "volume": 10}]
    tf5m = [
        {"time": today_utc_midnight_ms, "open": 100, "high": 110, "low": 90, "close": 105, "volume": 10}
        for _ in range(21) # Enough candles for average volume calc
    ]

    # Both engines should return a WAIT vote
    res_modular = modular_orb_engine(tf1h, tf15m, tf5m)
    assert res_modular["signal"] == "WAIT"
    assert "No 1H candle found for current UTC day" in res_modular["reason"]

    res_legacy = legacy_orb_engine(tf1h, tf15m, tf5m)
    assert res_legacy["signal"] == "WAIT"
    assert "No 1H candle found for current UTC day" in res_legacy["reason"]

def test_orb_engine_valid_candle_today():
    now = datetime.now(timezone.utc)
    today_utc_midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    today_utc_midnight_ms = int(today_utc_midnight.timestamp() * 1000)

    # First candle of the current UTC day is at UTC midnight
    tf1h = [
        {"time": today_utc_midnight_ms - 3600000, "open": 100, "high": 110, "low": 90, "close": 105, "volume": 10},
        {"time": today_utc_midnight_ms, "open": 105, "high": 120, "low": 100, "close": 115, "volume": 10}, # Opening range high: 120, low: 100
        {"time": today_utc_midnight_ms + 3600000, "open": 115, "high": 125, "low": 110, "close": 122, "volume": 10},
    ]
    # tf15m and tf5m break above high (120) with volume
    tf15m = [{"time": today_utc_midnight_ms + 4500000, "open": 115, "high": 125, "low": 115, "close": 122, "volume": 10}]

    # We want average volume of tf5m to be low, and the last candle's volume to be high
    # So 20 candles of volume 10, then the last candle (21st) of volume 100
    tf5m = [
        {"time": today_utc_midnight_ms + 4500000, "open": 115, "high": 125, "low": 115, "close": 122, "volume": 10}
        for _ in range(20)
    ]
    tf5m.append({"time": today_utc_midnight_ms + 4505000, "open": 115, "high": 125, "low": 115, "close": 122, "volume": 100})

    res_modular = modular_orb_engine(tf1h, tf15m, tf5m)
    assert res_modular["signal"] == "Buy"
    assert "1H opening range high broken" in res_modular["reason"]

    res_legacy = legacy_orb_engine(tf1h, tf15m, tf5m)
    assert res_legacy["signal"] == "Buy"
    assert "1H opening range high broken" in res_legacy["reason"]


def test_journal_engine_concurrency(tmp_path):
    import threading
    from engines.journal import JournalEngine

    # Use a temporary file for the journal path
    journal_file = tmp_path / "trade_journal.json"
    engine = JournalEngine(limit=10, path=journal_file)

    num_threads = 10
    entries_per_thread = 5

    def worker(worker_id):
        for i in range(entries_per_thread):
            engine.add("TEST_EVENT", {"worker_id": worker_id, "index": i})

    threads = []
    for t_id in range(num_threads):
        t = threading.Thread(target=worker, args=(t_id,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # The entries should be capped at limit=10
    assert len(engine.entries) <= 10

    # Let's verify that the entries loaded from file are also correct and match the in-memory state
    loaded_engine = JournalEngine(limit=10, path=journal_file)
    assert len(loaded_engine.entries) == len(engine.entries)
    for entry in loaded_engine.entries:
        assert entry["event"] == "TEST_EVENT"
        assert "worker_id" in entry["payload"]
        assert "index" in entry["payload"]
