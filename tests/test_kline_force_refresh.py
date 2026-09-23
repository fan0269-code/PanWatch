"""需要已完成日线的消费者不能把盘中缓存当作收盘数据。"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.platform.marketdata.collectors import kline_collector as kc
from src.platform.marketdata.models import MarketCode


def _bars(last_close=100.0):
    day = date(2026, 9, 23)
    rows = []
    while len(rows) < 60:
        if day.weekday() < 5:
            close = last_close if not rows else 100.0
            rows.append(kc.KlineData(date=day.isoformat(), open=close, close=close,
                                     high=close + 1, low=close - 1, volume=1000))
        day -= timedelta(days=1)
    return list(reversed(rows))


def test_force_refresh_replaces_intraday_cache_after_market_close(monkeypatch):
    clock = {"now": datetime(2026, 9, 23, 6, 59, tzinfo=timezone.utc).timestamp()}
    close_time = datetime(2026, 9, 23, 7, 0, tzinfo=timezone.utc).timestamp()
    source = {"rows": _bars(last_close=99.0)}
    monkeypatch.setattr(kc.time, "time", lambda: clock["now"])
    monkeypatch.setattr(kc, "_kline_cache_ttl", lambda _: 180 if clock["now"] < close_time else 1800)
    monkeypatch.setattr(kc, "get_market_data", lambda: SimpleNamespace(
        klines=lambda *args, **kwargs: source["rows"],
    ))
    collector = kc.KlineCollector(MarketCode.CN)
    assert collector.get_klines("600000")[-1].close == 99.0

    # 14:59 的盘中缓存到了 15:16 仍在收盘后 30 分钟 TTL 内。
    clock["now"] += 17 * 60
    source["rows"] = _bars(last_close=101.0)
    assert collector.get_klines("600000")[-1].close == 99.0
    refreshed = collector.get_klines("600000", force_refresh=True)
    assert refreshed[-1].close == 101.0
    assert collector.get_klines("600000")[-1].close == 101.0


def test_force_refresh_bypasses_failure_cooldown(monkeypatch):
    source = {"rows": []}
    monkeypatch.setattr(kc, "get_market_data", lambda: SimpleNamespace(
        klines=lambda *args, **kwargs: source["rows"],
    ))
    collector = kc.KlineCollector(MarketCode.CN)
    assert collector.get_klines("600000") == []
    source["rows"] = _bars(last_close=101.0)
    assert collector.get_klines("600000") == []
    assert collector.get_klines("600000", force_refresh=True)[-1].close == 101.0


@pytest.mark.parametrize("fetch_raises", [False, True])
def test_failed_force_refresh_never_returns_cached_bars(monkeypatch, fetch_raises):
    source = {"rows": _bars(last_close=99.0), "failed": False}

    def fetch(*args, **kwargs):
        if source["failed"]:
            if fetch_raises:
                raise RuntimeError("test data source unavailable")
            return []
        return source["rows"]

    monkeypatch.setattr(kc, "get_market_data", lambda: SimpleNamespace(klines=fetch))
    collector = kc.KlineCollector(MarketCode.CN)
    assert collector.get_klines("600000")[-1].close == 99.0
    source["failed"] = True
    if fetch_raises:
        with pytest.raises(RuntimeError, match="data source unavailable"):
            collector.get_klines("600000", force_refresh=True)
    else:
        assert collector.get_klines("600000", force_refresh=True) == []
