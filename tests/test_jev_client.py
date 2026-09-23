"""Jev 的 HTTP 契约、异常隔离和不可接受的概率响应。"""
import asyncio
import copy
import json

import httpx
import pytest

from src.platform.ai.jev_client import JevClient, JevError


QUESTIONS = {
    "trend": {
        "type": "choice",
        "instructions": "Classify the supplied trend evidence.",
        "criteria": {"strong": "Strong trend", "weak": "Weak trend"},
    }
}
RESPONSE = {
    "model": "jev-1.13.0",
    "answers": {"trend": {
        "type": "choice", "choice": "strong",
        "probabilities": {"strong": 0.8, "weak": 0.2}, "confidence": 0.6,
    }},
    "usage": {"input_tokens": 100, "output_tokens": 15},
}


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: original(**{**kwargs, "transport": httpx.MockTransport(handler), "trust_env": False}),
    )


def test_uses_systemone_contract_and_preserves_returned_model(monkeypatch):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=RESPONSE)

    transport(monkeypatch, handle)
    result = asyncio.run(JevClient("test-key").evaluate({"close": 10}, QUESTIONS))
    assert result == RESPONSE
    assert str(seen[0].url) == "https://api.typesafe.ai/v1/systemone"
    assert seen[0].headers["authorization"] == "Bearer test-key"
    assert json.loads(seen[0].content) == {
        "model": "jev-latest", "state": {"close": 10}, "questions": QUESTIONS,
    }


@pytest.mark.parametrize("mutate", [
    lambda x: x["answers"].clear(),
    lambda x: x["answers"]["trend"].update(choice="buy"),
    lambda x: x["answers"]["trend"].update(confidence=1.1),
    lambda x: x["answers"]["trend"].update(confidence=True),
    lambda x: x["answers"]["trend"].update(confidence=float("nan")),
    lambda x: x["answers"]["trend"].update(probabilities={"strong": 0.6, "weak": 0.1}),
    lambda x: x["answers"]["trend"].update(probabilities={"strong": 0.8, "other": 0.2}),
    lambda x: x["answers"]["trend"].update(choice="weak"),
    lambda x: x.update(model=""),
])
def test_rejects_unusable_answers(monkeypatch, mutate):
    body = copy.deepcopy(RESPONSE)
    mutate(body)
    transport(monkeypatch, lambda _: httpx.Response(200, content=json.dumps(body)))
    with pytest.raises(JevError):
        asyncio.run(JevClient("test-key").evaluate({}, QUESTIONS))


def test_missing_key_fails_without_http(monkeypatch):
    transport(monkeypatch, lambda _: pytest.fail("missing key must not reach network"))
    with pytest.raises(JevError) as error:
        asyncio.run(JevClient("").evaluate({}, QUESTIONS))
    assert error.value.status_code == 503


def test_auth_error_never_echoes_response_or_key(monkeypatch):
    transport(monkeypatch, lambda _: httpx.Response(401, text="test-key private request data"))
    with pytest.raises(JevError) as error:
        asyncio.run(JevClient("test-key").evaluate({}, QUESTIONS))
    assert "test-key" not in str(error.value)
    assert "private" not in str(error.value)


def test_timeout_is_not_retried(monkeypatch):
    seen = []

    def handle(request):
        seen.append(request)
        raise httpx.ReadTimeout("test-key private request data")

    transport(monkeypatch, handle)
    with pytest.raises(JevError) as error:
        asyncio.run(JevClient("test-key").evaluate({}, QUESTIONS))
    assert len(seen) == 1
    assert error.value.status_code == 504
    assert "test-key" not in str(error.value)


def test_rate_limit_retries_once_then_returns_error(monkeypatch):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(429, headers={"Retry-After": "0"})

    transport(monkeypatch, handle)
    with pytest.raises(JevError) as error:
        asyncio.run(JevClient("test-key").evaluate({}, QUESTIONS))
    assert len(seen) == 2
    assert error.value.status_code == 503


def test_rejects_invalid_input_before_sending(monkeypatch):
    transport(monkeypatch, lambda _: pytest.fail("invalid input must not reach network"))
    with pytest.raises(JevError):
        asyncio.run(JevClient("test-key").evaluate({"close": float("inf")}, QUESTIONS))
