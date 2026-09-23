"""Jev API 的配置隔离、输入校验与受保护路由。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.research.api import jev
from src.platform.persistence.database import Base, get_db
from src.web.response import ResponseWrapperMiddleware


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(jev.router, prefix="/api/jev")
    app.add_middleware(ResponseWrapperMiddleware)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    def test_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = test_db
    monkeypatch.setattr(jev, "Settings", lambda: SimpleNamespace(
        typesafe_api_key=SecretStr(""), jev_model="jev-test", jev_timeout_seconds=30, http_proxy="",
    ))
    with TestClient(app) as client:
        yield client
    engine.dispose()


def test_no_key_reports_unconfigured_and_returns_503_without_data_or_model_calls(client, monkeypatch):
    mocked = AsyncMock()
    monkeypatch.setattr(jev, "create_judgment", mocked)
    response = client.get("/api/jev/status")
    assert response.json()["data"] == {"configured": False, "model": "jev-test"}
    response = client.post("/api/jev/judgments", json={"symbol": "600000", "market": "CN"})
    assert response.status_code == 503
    mocked.assert_not_called()


def test_status_does_not_disclose_secret(client, monkeypatch):
    monkeypatch.setattr(jev, "Settings", lambda: SimpleNamespace(
        typesafe_api_key=SecretStr("secret-for-test"), jev_model="jev-test",
    ))
    response = client.get("/api/jev/status")
    assert response.json()["data"]["configured"] is True
    assert "secret-for-test" not in response.text


@pytest.mark.parametrize("override", [
    {"horizon": 2}, {"flat_threshold_pct": 0}, {"flat_threshold_pct": 11}, {"flat_threshold_pct": 0.05},
    {"market": "BAD"}, {"symbol": "../../x"}, {"analysis_id": -1},
])
def test_invalid_judgment_input_is_rejected(client, override):
    payload = {"symbol": "600000", "market": "CN", **override}
    assert client.post("/api/jev/judgments", json=payload).status_code == 422


def test_history_requires_market_and_bounded_limit(client):
    assert client.get("/api/jev/judgments?symbol=600000&market=CN").json()["data"] == []
    assert client.get("/api/jev/judgments?symbol=600000&market=BAD").status_code == 422
    assert client.get("/api/jev/judgments?symbol=600000&limit=500").status_code == 422


def test_data_errors_do_not_produce_success_or_fake_decisions(client, monkeypatch):
    monkeypatch.setattr(jev, "Settings", lambda: SimpleNamespace(
        typesafe_api_key=SecretStr("secret-for-test"), jev_model="jev-test", jev_timeout_seconds=30, http_proxy="",
    ))
    monkeypatch.setattr(jev, "create_judgment", AsyncMock(side_effect=jev.JevDataError("日线已过期")))
    response = client.post("/api/jev/judgments", json={"symbol": "600000", "market": "CN"})
    assert response.status_code == 422
    assert response.json()["data"] is None
    assert response.json()["success"] is False


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/jev/status", None),
    ("GET", "/api/jev/judgments?symbol=600000&market=CN", None),
    ("POST", "/api/jev/judgments", {"symbol": "600000", "market": "CN"}),
])
def test_configured_app_blocks_unauthenticated_jev_requests(monkeypatch, method, path, body):
    from src.bootstrap.application import app
    from src.modules.administration.api import auth

    # Use the real application route and real auth dependency, with an initialized
    # account. No lifespan is entered, so this does not start background tasks.
    monkeypatch.setattr(auth, "get_password_hash", lambda db: "stored-password-hash")
    response = TestClient(app).request(method, path, json=body)
    assert response.status_code == 401
    assert response.json()["success"] is False
