"""Jev 判断必须固定日线口径、保留逐次记录并隔离不同市场。"""

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from src.platform.persistence.database import Base
from src.platform.persistence.models import AnalysisHistory


def _bar(day: str, close: float = 100):
    return SimpleNamespace(date=day, open=close, high=close + 1, low=close - 1,
                           close=close, volume=1000)


def _bars(end: str, count: int = 80):
    current = date.fromisoformat(end)
    rows = []
    while len(rows) < count:
        if current.weekday() < 5:
            rows.append(_bar(current.isoformat(), 100 + len(rows) / 10))
        current -= timedelta(days=1)
    return list(reversed(rows))


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def test_completed_bars_exclude_intraday_and_future_and_use_market_timezone():
    from src.modules.research.jev_service import completed_daily_bars

    rows = [_bar("2026-09-21"), _bar("2026-09-22"), _bar("2026-09-23")]
    # UTC 19:00 = 美东 15:00，纽约当日K线尚未完成；上海已次日。
    now = datetime(2026, 9, 22, 19, 0, tzinfo=timezone.utc)
    assert [r.date for r in completed_daily_bars(rows, "US", now)] == ["2026-09-21"]
    assert [r.date for r in completed_daily_bars(rows, "CN", now)] == ["2026-09-21", "2026-09-22"]


def test_completed_bars_use_dst_and_conservative_close_buffer():
    from src.modules.research.jev_service import completed_daily_bars

    rows = [_bar("2026-01-12")]
    assert completed_daily_bars(rows, "US", datetime(2026, 1, 12, 21, 10, tzinfo=timezone.utc)) == []
    assert len(completed_daily_bars(rows, "US", datetime(2026, 1, 12, 21, 16, tzinfo=timezone.utc))) == 1


def test_market_context_rejects_insufficient_or_stale_completed_bars(monkeypatch):
    from src.modules.research import jev_service as service

    now = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(service.KlineCollector, "get_klines", lambda *args, **kwargs: _bars("2026-09-22", 10))
    with pytest.raises(service.JevDataError, match="至少 60"):
        service.build_market_evidence("600000", "CN", now)
    monkeypatch.setattr(service.KlineCollector, "get_klines", lambda *args, **kwargs: _bars("2026-09-01"))
    with pytest.raises(service.JevDataError, match="过期"):
        service.build_market_evidence("600000", "CN", now)


def test_indicators_receive_only_completed_bars(monkeypatch):
    from src.modules.research import jev_service as service

    rows = _bars("2026-09-22") + [_bar("2026-09-23", 999)]
    def fresh_bars(self, symbol, days, *, force_refresh=False):
        assert force_refresh is True
        return rows

    monkeypatch.setattr(service.KlineCollector, "get_klines", fresh_bars)
    context, _warnings = service.build_market_evidence(
        "600000", "CN", datetime(2026, 9, 23, 5, tzinfo=timezone.utc)
    )
    assert context["as_of"] == "2026-09-22"
    assert context["reference_price"] == 100
    assert context["indicators"]["ma5"] < 110
    assert all(row["date"] < "2026-09-23" for row in context["daily_bars"])


def _analysis(db, market=None, symbol="600000", agent="daily_report"):
    raw_data = {"market": market} if market else {}
    row = AnalysisHistory(agent_name=agent, stock_symbol=symbol, analysis_date="2026-09-22",
                          title=f"{market}报告", content="均线偏强", raw_data=raw_data)
    db.add(row)
    db.commit()
    return row


def test_report_review_never_borrows_same_symbol_from_another_market(db):
    from src.modules.research.jev_service import select_review_source

    hk = _analysis(db, "HK")
    _analysis(db, None, agent="chart_analyst")
    row, warnings = select_review_source(db, "600000", "CN")
    assert row is None
    assert warnings
    row, warnings = select_review_source(db, "600000", "CN", hk.id)
    assert row is None
    assert warnings
    cn = _analysis(db, "CN", agent="tradingagents")
    row, warnings = select_review_source(db, "600000", "CN")
    assert row.id == cn.id


def test_report_source_without_metadata_is_skipped(db):
    from src.modules.research.jev_service import select_review_source

    unknown = _analysis(db)
    row, warnings = select_review_source(db, "600000", "CN", unknown.id)
    assert row is None
    assert any("市场" in warning for warning in warnings)


class FakeClient:
    def __init__(self):
        self.calls = []

    async def evaluate(self, state, questions):
        self.calls.append((state, questions))
        choices = {"trend": "neutral", "risk": "medium", "direction": "flat", "review": "partial"}
        return {"model": "jev-test", "answers": {
            key: {"type": "choice", "choice": choices[key], "confidence": 0.7,
                  "probabilities": {choice: (0.7 if choice == choices[key] else 0.1)
                                    for choice in question["criteria"]}}
            for key, question in questions.items()}, "usage": {"input_tokens": 100}}


def test_judgments_are_append_only_market_scoped_and_no_report_skips_review(db, monkeypatch):
    from src.modules.research import jev_service as service
    from src.modules.research.jev_schemas import JudgmentRequest
    from src.platform.persistence.models import JevJudgment

    monkeypatch.setattr(service.KlineCollector, "get_klines", lambda *args, **kwargs: _bars("2026-09-22"))
    client = FakeClient()
    now = datetime(2026, 9, 23, 5, tzinfo=timezone.utc)
    for market in ("CN", "CN", "HK"):
        result = asyncio.run(service.create_judgment(
            JudgmentRequest(symbol="600000", market=market, horizon=3), db, client, now=now
        ))
        assert result["decisions"]["review"] is None
        assert result["review_source"] is None
        assert result["warnings"]
    records = service.list_judgments(db, "600000", "CN", 10)
    assert len(records) == 2
    assert records[0]["id"] != records[1]["id"]
    assert db.query(JevJudgment).count() == 3
    assert all("review" not in questions for _, questions in client.calls)
    saved = db.query(JevJudgment).first()
    assert saved.input_snapshot["state"]["forecast"]["horizon_trading_days"] == 3
    assert saved.input_snapshot["version"] == service.JUDGMENT_VERSION
    assert "api_key" not in str(saved.input_snapshot)


def test_review_is_of_current_supplied_evidence_and_snapshots_source(db, monkeypatch):
    from src.modules.research import jev_service as service
    from src.modules.research.jev_schemas import JudgmentRequest

    source = _analysis(db, "CN")
    monkeypatch.setattr(service.KlineCollector, "get_klines", lambda *args, **kwargs: _bars("2026-09-22"))
    client = FakeClient()
    result = asyncio.run(service.create_judgment(
        JudgmentRequest(symbol="600000", market="CN", analysis_id=source.id), db, client,
        now=datetime(2026, 9, 23, 5, tzinfo=timezone.utc)
    ))
    assert result["decisions"]["review"]["choice"] == "partial"
    assert result["review_source"]["id"] == source.id
    assert len(client.calls) == 2
    assert "review_source" not in client.calls[0][0]
    assert "review" not in client.calls[0][1]
    assert client.calls[1][0]["review_source"]["content"] == "均线偏强"
    assert set(client.calls[1][1]) == {"review"}
    assert any("不是历史时点审计" in warning for warning in result["warnings"])
    assert "累计收益" in client.calls[0][1]["direction"]["instructions"]


def test_new_tradingagents_reports_preserve_explicit_market_identity(db):
    from src.modules.automation.tradingagents.decision import map_state_to_result
    from src.modules.research.jev_service import select_review_source
    from src.platform.marketdata.models import MarketCode

    result = map_state_to_result(
        stock=SimpleNamespace(symbol="600000", name="测试", market=MarketCode.CN),
        ta_result={"decision": "HOLD", "final_state": {"final_trade_decision": "最终决策：Hold"}},
    )
    assert result.raw_data["stock"] == {"symbol": "600000", "market": "CN"}
    row = _analysis(db, agent="tradingagents")
    row.raw_data = result.raw_data
    db.commit()
    selected, _ = select_review_source(db, "600000", "CN")
    assert selected.id == row.id


def test_new_migration_is_idempotent_and_registers_indexes():
    from src.platform.persistence.migrations import _m127_jev_judgments

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _m127_jev_judgments(connection)
        _m127_jev_judgments(connection)
    assert "jev_judgments" in inspect(engine).get_table_names()
    assert "ix_jev_judgments_stock_created" in {row["name"] for row in inspect(engine).get_indexes("jev_judgments")}


def test_upgrade_preserves_existing_analysis_and_allows_new_judgment_rows():
    from src.platform.persistence.migrations import _m127_jev_judgments
    from src.platform.persistence.models import JevJudgment

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables
                                             if table.name != "jev_judgments"])
    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        original = AnalysisHistory(
            agent_name="daily_report", stock_symbol="600000", analysis_date="2026-09-22",
            title="原有分析", content="历史正文不能覆盖", raw_data={"market": "CN", "important": [1, 2, 3]},
        )
        session.add(original)
        session.commit()
        original_id = original.id
    with engine.begin() as connection:
        _m127_jev_judgments(connection)
        _m127_jev_judgments(connection)
    with session_factory() as session:
        old = session.get(AnalysisHistory, original_id)
        assert old.title == "原有分析"
        assert old.content == "历史正文不能覆盖"
        assert old.raw_data == {"market": "CN", "important": [1, 2, 3]}
        session.add(JevJudgment(
            symbol="600000", market="CN", as_of="2026-09-22", reference_price=100,
            horizon=3, flat_threshold_pct=1, model="jev-test", input_snapshot={}, result={},
        ))
        session.commit()
        assert session.query(AnalysisHistory).count() == 1
        assert session.query(JevJudgment).count() == 1
    engine.dispose()
