"""飞书应用机器人协议测试；全部网络请求使用 MockTransport。"""

import asyncio
import json
import logging

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.platform.notifications.notifier import NotifierManager
from src.platform.notifications.notify_policy import NotifyPolicy
from src.platform.persistence.database import Base
from src.platform.persistence.models import NotifyChannel

_REAL_NOTIFY = NotifierManager.notify_with_result
_REAL_CLIENT = httpx.AsyncClient
CONFIG = {"app_id": "cli_test_app", "app_secret": "fake-test-secret", "receive_id": "oc_test_group"}
TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
TOKEN = "t-fake-test-token"


def token_response(token=TOKEN, expire=7200):
    return httpx.Response(200, json={"code": 0, "tenant_access_token": token, "expire": expire})


def message_response():
    return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_test_message"}})


@pytest.fixture
def transport(monkeypatch):
    requests = []

    def install(handler=None):
        async def dispatch(request):
            requests.append(request)
            if handler is not None:
                result = handler(request)
                if asyncio.iscoroutine(result):
                    result = await result
                return result
            return token_response() if str(request.url) == TOKEN_URL else message_response()

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _REAL_CLIENT(
            transport=httpx.MockTransport(dispatch), **kwargs,
        ))
        monkeypatch.setattr(NotifierManager, "notify_with_result", _REAL_NOTIFY)
        return requests

    return install


def manager(config=None, *, retry_attempts=0):
    result = NotifierManager(policy=NotifyPolicy(retry_attempts=retry_attempts, retry_backoff_seconds=0))
    result.add_channel("feishu_app", CONFIG if config is None else config)
    return result


def test_application_channel_uses_tenant_token_and_serialized_plaintext(transport):
    requests = transport()
    result = asyncio.run(manager().notify_with_result("**标题**", "## 报告\n[详情](https://example.com/report_daily_full)"))
    assert result == {"success": True}
    assert len(requests) == 2
    assert str(requests[0].url) == TOKEN_URL
    assert json.loads(requests[0].content) == {"app_id": CONFIG["app_id"], "app_secret": CONFIG["app_secret"]}
    message = requests[1]
    assert str(message.url) == MESSAGE_URL + "?receive_id_type=chat_id"
    assert message.headers["Authorization"] == "Bearer " + TOKEN
    payload = json.loads(message.content)
    assert payload["receive_id"] == CONFIG["receive_id"]
    assert payload["msg_type"] == "text"
    assert isinstance(payload["content"], str)
    assert json.loads(payload["content"])["text"] == "标题\n\n报告\n详情 https://example.com/report_daily_full"
    assert 1 <= len(payload["uuid"]) <= 50


@pytest.mark.parametrize("change", [
    {"app_id": "bad"}, {"app_id": "cli_"}, {"app_id": "cli_" + "a" * 125},
    {"app_secret": ""}, {"app_secret": "a b"}, {"app_secret": "a" * 257},
    {"receive_id_type": "email"}, {"receive_id_type": None},
    {"receive_id": ""}, {"receive_id": "http://127.0.0.1/"},
    {"receive_id": "ou_user"}, {"receive_id": "oc_"},
    {"receive_id_type": "open_id", "receive_id": "oc_group"},
    {"receive_id_type": "user_id", "receive_id": "a" * 129},
])
def test_invalid_configuration_rejected_without_secrets(change):
    with pytest.raises(ValueError) as exc:
        manager({**CONFIG, **change})
    assert CONFIG["app_secret"] not in str(exc.value)


@pytest.mark.parametrize("id_type,receiver", [("chat_id", "oc_group"), ("open_id", "ou_user"), ("user_id", "staff_1")])
def test_receiver_types_and_trim_are_preserved(transport, id_type, receiver):
    requests = transport()
    config = {"app_id": " cli_test_app ", "app_secret": " fake-test-secret ", "receive_id_type": " " + id_type + " ", "receive_id": " " + receiver + " ", "endpoint": "http://localhost"}
    assert asyncio.run(manager(config).notify_with_result("", "正文"))["success"]
    assert requests[-1].url.params["receive_id_type"] == id_type
    assert json.loads(requests[-1].content)["receive_id"] == receiver
    assert requests[-1].url.host == "open.feishu.cn"


def test_concurrent_sends_share_one_token_and_use_distinct_uuids(transport):
    async def handler(request):
        if str(request.url) == TOKEN_URL:
            await asyncio.sleep(0.01)
            return token_response()
        return message_response()

    requests = transport(handler)

    async def run():
        return await asyncio.gather(*(manager().notify_with_result("标题", "正文") for _ in range(8)))

    assert all(result["success"] for result in asyncio.run(run()))
    assert len([r for r in requests if str(r.url) == TOKEN_URL]) == 1
    messages = [json.loads(r.content) for r in requests if r.url.path.endswith("/messages")]
    assert len({m["uuid"] for m in messages}) == 8


def test_cache_isolated_by_credentials_and_reused_across_targets(transport):
    requests = transport()

    async def run():
        for config in [CONFIG, {**CONFIG, "receive_id": "oc_other"}, {**CONFIG, "app_id": "cli_another"}, {**CONFIG, "app_secret": "rotated-test-secret"}]:
            assert (await manager(config).notify_with_result("", "正文"))["success"]

    asyncio.run(run())
    assert len([r for r in requests if str(r.url) == TOKEN_URL]) == 3


def test_cache_expires_early_and_never_uses_stale_token_after_refresh_failure(transport, monkeypatch):
    from src.platform.notifications import feishu_app

    now = [1000.0]
    monkeypatch.setattr(feishu_app, "_now", lambda: now[0])
    token_calls = [0]

    def handler(request):
        if str(request.url) == TOKEN_URL:
            token_calls[0] += 1
            if token_calls[0] == 2:
                return httpx.Response(500, text="upstream failure")
            return token_response(expire=100)
        return message_response()

    requests = transport(handler)

    async def run():
        notifier = manager()
        assert (await notifier.notify_with_result("", "正文"))["success"]
        now[0] = 1089
        assert (await notifier.notify_with_result("", "正文"))["success"]
        now[0] = 1091
        assert not (await notifier.notify_with_result("", "正文"))["success"]
        assert (await notifier.notify_with_result("", "正文"))["success"]

    asyncio.run(run())
    assert token_calls[0] == 3
    assert len([r for r in requests if r.url.path.endswith("/messages")]) == 3


@pytest.mark.parametrize("code", [99991663, 99991665])
def test_invalid_token_refreshes_once_using_same_uuid_even_with_policy_retries(transport, code):
    token_calls = [0]
    def handler(request):
        if str(request.url) == TOKEN_URL:
            token_calls[0] += 1
            return token_response("t-token-" + str(token_calls[0]))
        return httpx.Response(400, json={"code": code, "msg": "do not echo"})

    requests = transport(handler)
    result = asyncio.run(manager(retry_attempts=5).notify_with_result("", "正文"))
    assert result["success"] is False
    assert token_calls[0] == 2
    messages = [r for r in requests if r.url.path.endswith("/messages")]
    assert len(messages) == 2
    assert json.loads(messages[0].content)["uuid"] == json.loads(messages[1].content)["uuid"]
    assert messages[0].headers["Authorization"] != messages[1].headers["Authorization"]


def test_token_refresh_succeeds_and_refreshed_token_is_cached(transport):
    token_calls = [0]
    def handler(request):
        if str(request.url) == TOKEN_URL:
            token_calls[0] += 1
            return token_response("t-token-" + str(token_calls[0]))
        if request.headers["Authorization"] == "Bearer t-token-1":
            return httpx.Response(401, json={"code": 99991663})
        return message_response()

    requests = transport(handler)

    async def run():
        notifier = manager()
        assert (await notifier.notify_with_result("", "正文"))["success"]
        assert (await notifier.notify_with_result("", "正文"))["success"]

    asyncio.run(run())
    assert token_calls[0] == 2
    messages = [json.loads(r.content) for r in requests if r.url.path.endswith("/messages")]
    assert messages[0]["uuid"] == messages[1]["uuid"]
    assert messages[1]["uuid"] != messages[2]["uuid"]


def test_late_invalid_response_does_not_remove_concurrently_refreshed_token(transport):
    async def run():
        old_requests_arrived = asyncio.Event()
        new_token_sent = asyncio.Event()
        token_calls = 0
        old_message_calls = 0

        async def handler(request):
            nonlocal token_calls, old_message_calls
            if str(request.url) == TOKEN_URL:
                token_calls += 1
                return token_response("t-token-" + str(token_calls))
            if request.headers["Authorization"] == "Bearer t-token-1":
                old_message_calls += 1
                if old_message_calls == 1:
                    await old_requests_arrived.wait()
                else:
                    old_requests_arrived.set()
                    await new_token_sent.wait()
                return httpx.Response(400, json={"code": 99991663})
            new_token_sent.set()
            return message_response()

        requests = transport(handler)
        results = await asyncio.wait_for(asyncio.gather(
            manager().notify_with_result("", "第一条"),
            manager().notify_with_result("", "第二条"),
        ), timeout=2)
        assert all(result["success"] for result in results)
        assert token_calls == 2
        assert len([r for r in requests if r.url.path.endswith("/messages")]) == 4

    asyncio.run(run())


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"code": 99991672, "msg": CONFIG["app_secret"]}),
    httpx.Response(400, json={"code": 230002, "msg": TOKEN}),
    httpx.Response(200, json={"code": 230006, "msg": TOKEN}),
    httpx.Response(500, json={"code": 0}),
    httpx.Response(200, json={"code": False}),
    httpx.Response(200, json={"code": "0"}),
    httpx.Response(200, json={"code": 0, "data": {}}),
    httpx.Response(200, text=CONFIG["app_secret"]),
    httpx.Response(302, headers={"location": "http://127.0.0.1"}),
])
def test_message_errors_do_not_retry_or_leak(transport, response, caplog):
    requests = transport(lambda r: token_response() if str(r.url) == TOKEN_URL else response)
    result = asyncio.run(manager(retry_attempts=5).notify_with_result("", "private-body"))
    assert result["success"] is False
    assert len(requests) == 2
    assert TOKEN not in result["error"] + caplog.text
    assert CONFIG["app_secret"] not in result["error"] + caplog.text


@pytest.mark.parametrize("data", [
    {"code": 10015, "msg": CONFIG["app_secret"]},
    {"code": 0, "tenant_access_token": "", "expire": 7200},
    {"code": 0, "tenant_access_token": TOKEN, "expire": 0},
    {"code": 0, "tenant_access_token": TOKEN, "expire": True},
    {"code": 0, "tenant_access_token": TOKEN, "expire": 7201},
    {"code": 0, "tenant_access_token": "t-bad\nheader", "expire": 7200},
])
def test_bad_token_response_never_sends_message(transport, data):
    requests = transport(lambda r: httpx.Response(200, json=data))
    result = asyncio.run(manager().notify_with_result("", "正文"))
    assert result["success"] is False
    assert len(requests) == 1
    assert CONFIG["app_secret"] not in result["error"]
    assert TOKEN not in result["error"]


def test_http_debug_logs_and_network_exceptions_cannot_reveal_credentials(transport, caplog):
    caplog.set_level(logging.DEBUG)
    def handler(request):
        logging.getLogger("httpcore.http11").debug("request=%s auth=%s", request.content, request.headers)
        logging.getLogger("httpx").info("fake response token=%s", TOKEN)
        raise httpx.ConnectError(CONFIG["app_secret"] + TOKEN + "private-body")

    transport(handler)
    result = asyncio.run(manager(retry_attempts=3).notify_with_result("private-title", "private-body"))
    assert not result["success"]
    combined = caplog.text + result["error"]
    for value in [CONFIG["app_secret"], TOKEN, "private-title", "private-body"]:
        assert value not in combined
    logging.getLogger("httpx").info("unrelated request remains visible")
    assert "unrelated request remains visible" in caplog.text


def test_http_log_privacy_is_local_to_sending_task(transport, caplog):
    caplog.set_level(logging.DEBUG)

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(request):
            if str(request.url) == TOKEN_URL:
                logging.getLogger("httpx").info("private auth %s", CONFIG["app_secret"])
                entered.set()
                await release.wait()
                return token_response()
            logging.getLogger("httpcore.http11").debug("response authorization %s", TOKEN)
            return message_response()

        transport(handler)
        sending = asyncio.create_task(manager().notify_with_result("", "正文"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        logging.getLogger("httpx").info("concurrent unrelated request")
        release.set()
        assert (await sending)["success"]

    asyncio.run(run())
    assert "concurrent unrelated request" in caplog.text
    assert CONFIG["app_secret"] not in caplog.text
    assert TOKEN not in caplog.text


def test_large_unicode_payload_is_truncated_below_api_limit(transport):
    requests = transport()
    assert asyncio.run(manager().notify_with_result("报告", '行情😀\\\n"' * 30_000))["success"]
    assert len(requests[-1].content) <= 150_000
    text = json.loads(json.loads(requests[-1].content)["content"])["text"]
    assert "截断" in text


def test_api_validates_config_before_mutations_and_test_endpoint_uses_actual_result(transport):
    from src.modules.administration.api.channels import ChannelCreate, ChannelUpdate, create_channel, update_channel, test_channel as send_test

    transport(lambda r: token_response() if str(r.url) == TOKEN_URL else httpx.Response(400, json={"code": 230002}))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        old = NotifyChannel(name="旧渠道", type="lark", config={"webhook_token": "legacy"}, is_default=True)
        db.add(old)
        db.commit()
        with pytest.raises(HTTPException) as exc:
            create_channel(ChannelCreate(name="应用", type="feishu_app", config={}, is_default=True), db)
        assert exc.value.status_code == 400
        assert old.is_default is True and db.query(NotifyChannel).count() == 1
        new = create_channel(ChannelCreate(name="应用", type="feishu_app", config=CONFIG), db)
        assert new.config["receive_id_type"] == "chat_id"
        with pytest.raises(HTTPException):
            update_channel(new.id, ChannelUpdate(config={**CONFIG, "app_secret": ""}), db)
        db.refresh(new)
        assert new.config["app_secret"] == CONFIG["app_secret"]
        with pytest.raises(HTTPException) as exc:
            asyncio.run(send_test(new.id, db))
        assert exc.value.status_code == 500
        assert "230002" in exc.value.detail
    engine.dispose()
