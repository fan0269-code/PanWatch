"""国内飞书机器人：签名、业务响应、URL限制与密钥不进入错误/日志。"""

import asyncio
import json
import logging

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.platform.notifications.notifier import NotifierManager, build_apprise_url
from src.platform.persistence.database import Base
from src.platform.persistence.models import NotifyChannel

_REAL_NOTIFY = NotifierManager.notify_with_result
_REAL_CLIENT = httpx.AsyncClient
HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/test-token-123"
SECRET = "test-signing-secret"


@pytest.fixture
def mocked_transport(monkeypatch):
    requests = []

    def install(response=None, *, error=None):
        def handler(request):
            requests.append(request)
            if error is not None:
                raise error
            return response or httpx.Response(200, json={"code": 0, "msg": "success"})

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _REAL_CLIENT(
            transport=httpx.MockTransport(handler), **kwargs
        ))
        monkeypatch.setattr(NotifierManager, "notify_with_result", _REAL_NOTIFY)
        return requests

    return install


def test_feishu_sign_matches_official_timestamp_demo_vector():
    from src.platform.notifications.feishu import sign_request

    assert sign_request(1599360473, "demo") == "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8="


@pytest.mark.parametrize("url", [
    "http://open.feishu.cn/open-apis/bot/v2/hook/test-token",
    "https://127.0.0.1/open-apis/bot/v2/hook/test-token",
    "https://open.feishu.cn.evil.example/open-apis/bot/v2/hook/test-token",
    "https://open.feishu.cn@127.0.0.1/open-apis/bot/v2/hook/test-token",
    "https://open.feishu.cn:443/open-apis/bot/v2/hook/test-token",
    HOOK + "?target=http://localhost", HOOK + "#fragment", HOOK + "/",
    HOOK + "%2f..", "https://open.feishu.cn/anything-else",
    "https://open.feishu.cn/open-apis/bot/v2/hook/" + "a" * 129,
])
def test_webhook_validation_rejects_noncanonical_targets_without_echoing(url):
    from src.platform.notifications.feishu import validate_config

    with pytest.raises(ValueError) as exc:
        validate_config({"webhook_url": url, "secret": SECRET})
    assert url not in str(exc.value)
    assert SECRET not in str(exc.value)


def test_plaintext_and_markdown_are_sent_signed_to_domestic_endpoint(mocked_transport, monkeypatch, caplog):
    from src.platform.notifications import feishu

    requests = mocked_transport()
    monkeypatch.setattr(feishu.time, "time", lambda: 1599360473)
    caplog.set_level(logging.INFO, logger="httpx")
    manager = NotifierManager()
    manager.add_channel("feishu", {"webhook_url": HOOK, "secret": "demo"})
    result = asyncio.run(manager.notify_with_result("**测试标题**", "## 指标\n**均线向上** [详情](https://example.com/report_daily_full)"))
    assert result == {"success": True}
    assert len(requests) == 1
    assert str(requests[0].url) == HOOK
    payload = json.loads(requests[0].content)
    assert payload["timestamp"] == "1599360473"
    assert payload["sign"] == "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8="
    assert payload["msg_type"] == "text"
    assert "均线向上" in payload["content"]["text"]
    assert "**" not in payload["content"]["text"]
    assert "https://example.com/report_daily_full" in payload["content"]["text"]
    assert "test-token-123" not in caplog.text
    assert "secret" not in payload


def test_unsigned_requests_do_not_include_signature(mocked_transport):
    requests = mocked_transport()
    manager = NotifierManager()
    manager.add_channel("feishu", {"webhook_url": HOOK})
    assert asyncio.run(manager.notify_with_result("标题", "纯文本"))["success"]
    payload = json.loads(requests[0].content)
    assert "timestamp" not in payload and "sign" not in payload


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"code": 19021, "msg": HOOK + SECRET}),
    httpx.Response(200, json={"code": "0"}),
    httpx.Response(200, json={"code": False}),
    httpx.Response(200, json={"msg": "success"}),
    httpx.Response(200, text="not-json " + HOOK + SECRET),
    httpx.Response(500, text=HOOK + SECRET),
    httpx.Response(302, headers={"location": "http://127.0.0.1/"}),
])
def test_transport_and_business_failures_are_safe_and_not_success(mocked_transport, response, caplog):
    requests = mocked_transport(response)
    manager = NotifierManager()
    manager.add_channel("feishu", {"webhook_url": HOOK, "secret": SECRET})
    result = asyncio.run(manager.notify_with_result("测试", "正文"))
    assert result["success"] is False
    assert len(requests) == 1
    assert HOOK not in result["error"] and SECRET not in result["error"]
    assert "test-token-123" not in caplog.text and SECRET not in caplog.text


def test_network_exception_does_not_expose_webhook_or_secret(mocked_transport, caplog):
    mocked_transport(error=httpx.ConnectError(HOOK + SECRET))
    manager = NotifierManager()
    manager.add_channel("feishu", {"webhook_url": HOOK, "secret": SECRET})
    result = asyncio.run(manager.notify_with_result("测试", "正文"))
    assert result["success"] is False
    assert HOOK not in result["error"] and SECRET not in result["error"]
    assert "test-token-123" not in caplog.text and SECRET not in caplog.text


def test_large_unicode_notification_respects_20kb_json_limit(mocked_transport):
    requests = mocked_transport()
    manager = NotifierManager()
    manager.add_channel("feishu", {"webhook_url": HOOK, "secret": SECRET})
    result = asyncio.run(manager.notify_with_result("行情", "中文😀\n\"" * 20_000))
    assert result["success"] is True
    assert len(requests[0].content) <= 20_000
    assert "截断" in json.loads(requests[0].content)["content"]["text"]


def test_lark_existing_channel_still_uses_original_token_contract():
    assert build_apprise_url("lark", {"webhook_token": "legacy-token"}) == "lark://legacy-token/"


def test_httpcore_debug_headers_are_redacted_before_log_handlers(mocked_transport, caplog):
    mocked_transport()
    manager = NotifierManager()
    manager.add_channel("feishu", {"webhook_url": HOOK})
    asyncio.run(manager.notify_with_result("标题", "正文"))
    caplog.set_level(logging.DEBUG, logger="httpcore.http11")
    logging.getLogger("httpcore.http11").debug("headers=%s", [(b"location", HOOK.encode())])
    assert "test-token-123" not in caplog.text


def test_channel_test_endpoint_checks_real_business_result_with_mock_transport(mocked_transport):
    from src.modules.administration.api.channels import test_channel as send_test

    mocked_transport(httpx.Response(200, json={"code": 19021, "msg": HOOK + SECRET}))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        channel = NotifyChannel(name="飞书", type="feishu", config={"webhook_url": HOOK, "secret": SECRET})
        db.add(channel)
        db.commit()
        with pytest.raises(HTTPException) as exc:
            asyncio.run(send_test(channel.id, db))
        assert exc.value.status_code == 500
        assert "19021" in exc.value.detail
        assert HOOK not in exc.value.detail and SECRET not in exc.value.detail
    engine.dispose()


def test_api_checks_config_before_persisting_or_changing_default():
    from src.modules.administration.api.channels import ChannelCreate, ChannelUpdate, create_channel, update_channel

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        existing = NotifyChannel(name="原有渠道", type="lark", config={"webhook_token": "legacy"}, is_default=True)
        db.add(existing)
        db.commit()
        with pytest.raises(HTTPException) as exc:
            create_channel(ChannelCreate(name="飞书", type="feishu", config={"webhook_url": "http://localhost/"}, is_default=True), db)
        assert exc.value.status_code == 400
        assert db.query(NotifyChannel).count() == 1
        assert existing.is_default is True
        created = create_channel(ChannelCreate(name="飞书", type="feishu", config={"webhook_url": " " + HOOK + " ", "secret": SECRET}), db)
        assert created.config["webhook_url"] == HOOK
        with pytest.raises(HTTPException):
            update_channel(created.id, ChannelUpdate(config={"webhook_url": "http://localhost/"}), db)
        db.refresh(created)
        assert created.config["webhook_url"] == HOOK
