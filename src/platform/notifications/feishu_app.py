"""国内飞书自建应用机器人：租户令牌与纯文本消息。

协议：/document/server-docs/authentication-management/access-token/tenant_access_token_internal
      /document/server-docs/im-v1/message/create
令牌只在当前进程内缓存，通知只对明确的令牌失效刷新重试一次。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import logging
import re
from time import monotonic as _now
from uuid import uuid4
from weakref import WeakKeyDictionary, WeakValueDictionary

import httpx

_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
_INVALID_TOKEN_CODES = {99991663, 99991665}
_MAX_CACHED_TOKENS = 64
_MAX_REQUEST_BYTES = 150_000
_TRUNCATION_NOTICE = "\n\n…内容过长已截断，完整报告请在 PanWatch 查看。"
_SENSITIVE_REQUEST: ContextVar[bool] = ContextVar("feishu_app_request", default=False)


def validate_config(config: dict) -> dict[str, str]:
    """规范化配置，校验错误不可回显任何凭据或用户提交值。"""
    if not isinstance(config, dict):
        raise ValueError("飞书应用渠道配置必须为对象")
    values = {}
    for key in ("app_id", "app_secret", "receive_id_type", "receive_id"):
        value = config.get(key, "chat_id" if key == "receive_id_type" else "")
        if not isinstance(value, str):
            raise ValueError("飞书应用配置字段必须为字符串")
        values[key] = value.strip()
    if not re.fullmatch(r"cli_[A-Za-z0-9_-]{1,124}", values["app_id"]):
        raise ValueError("飞书 App ID 需要以 cli_ 开头，长度不能超过 128 字符")
    if not re.fullmatch(r"[!-~]{1,256}", values["app_secret"]):
        raise ValueError("飞书 App Secret 必填，须为 1 至 256 个非空白 ASCII 可打印字符")
    kind, receiver = values["receive_id_type"], values["receive_id"]
    if kind not in {"chat_id", "open_id", "user_id"}:
        raise ValueError("飞书接收 ID 类型仅支持 chat_id、open_id 或 user_id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", receiver):
        raise ValueError("飞书接收 ID 必填，须为 1 至 128 个字母、数字、下划线或短横线")
    prefix = {"chat_id": "oc_", "open_id": "ou_"}.get(kind)
    if prefix and (not receiver.startswith(prefix) or len(receiver) <= len(prefix)):
        raise ValueError(f"飞书 {kind} 需要以 {prefix} 开头并包含有效 ID")
    return values


class FeishuAppError(RuntimeError):
    """仅持有安全错误文本和业务码，禁止保存原始响应。"""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class _PrivateHTTPLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # httpcore DEBUG 可记录响应头和异常，不能让它们进入数据库日志。
        # ContextVar 只影响本协程及其子任务，不会关闭其他通知的日志。
        return not _SENSITIVE_REQUEST.get()


_HTTP_LOG_FILTER = _PrivateHTTPLog()


@dataclass
class _Token:
    value: str = field(repr=False)
    deadline: float


@dataclass
class _TokenCache:
    tokens: OrderedDict[str, _Token] = field(default_factory=OrderedDict, repr=False)
    # 弱锁在等待者全部退出后释放，避免锁引用事件循环而让整个缓存无法回收。
    locks: WeakValueDictionary = field(default_factory=WeakValueDictionary, repr=False)


_CACHES: WeakKeyDictionary = WeakKeyDictionary()


def _cache_for_loop() -> _TokenCache:
    loop = asyncio.get_running_loop()
    if loop not in _CACHES:
        _CACHES[loop] = _TokenCache()
    return _CACHES[loop]


def _business_error(code: int) -> FeishuAppError:
    hint = {
        10005: "应用或租户信息无效，请核对 App ID",
        10014: "应用未授权或不可用，请检查应用发布状态",
        10015: "App Secret 无效，请核对应用凭据",
        99991662: "应用未启用，请检查应用发布状态",
        99991663: "租户访问令牌无效，请稍后重试",
        99991665: "租户访问令牌无效，请稍后重试",
        99991672: "缺少权限，请为应用开通 im:message:send_as_bot；user_id 还需 contact:user.employee_id:readonly",
        99991676: "令牌缺少所需权限，请检查应用权限并发布版本",
        99991400: "请求过于频繁，请稍后重试",
        99991401: "服务器 IP 不在应用允许列表中",
        99991403: "应用 API 调用额度已用尽",
        230002: "机器人不在目标群中，请先将应用机器人加入群聊",
        230006: "应用未启用机器人能力，请启用后发布版本",
        230013: "目标用户不在机器人可用范围内或已停用",
        230018: "群聊设置不允许机器人发言",
        230020: "发送过于频繁，请稍后重试",
        230022: "消息内容未通过平台检查",
        230025: "消息内容超过平台大小限制",
        230027: "缺少必要权限或外部群共享未开启",
        230028: "消息未通过数据防泄漏检查",
        230029: "目标用户已离职",
        230034: "接收 ID 无效，请检查接收 ID 和类型是否匹配",
        230035: "平台拒绝向该目标发送消息，请检查群或用户设置",
        230038: "不支持向其他租户的用户发送单聊消息",
        230049: "消息仍在发送中，请稍后检查接收结果",
        230053: "目标用户已停用机器人消息",
        232009: "目标群聊已解散",
    }.get(code, "请检查应用发布状态、机器人权限及接收目标")
    return FeishuAppError(f"飞书应用业务错误 {code}：{hint}", code=code)


def _read_response(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        data = None
    if isinstance(data, dict):
        code = data.get("code")
        if type(code) is int and 0 < code <= 2_147_483_647:
            raise _business_error(code)
    if response.status_code != 200:
        raise FeishuAppError(f"飞书应用服务返回 HTTP {response.status_code}")
    if not isinstance(data, dict) or type(data.get("code")) is not int or data["code"] != 0:
        raise FeishuAppError("飞书应用服务响应缺少有效业务状态码")
    return data


async def _get_token(client: httpx.AsyncClient, config: dict, cache: _TokenCache, key: str) -> str:
    lock = cache.locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        cache.locks[key] = lock
    async with lock:
        now = _now()
        cached = cache.tokens.get(key)
        if cached and cached.deadline > now:
            cache.tokens.move_to_end(key)
            return cached.value
        cache.tokens.pop(key, None)
        response = await client.post(
            _TOKEN_URL, json={"app_id": config["app_id"], "app_secret": config["app_secret"]},
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        data = _read_response(response)
        token, expire = data.get("tenant_access_token"), data.get("expire")
        if (
            not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,4096}", token)
            or type(expire) is not int or not 0 < expire <= 7200
        ):
            raise FeishuAppError("飞书应用鉴权响应缺少有效租户令牌或有效期")
        deadline = now + expire - min(120, expire * 0.1)
        if deadline <= _now():
            raise FeishuAppError("飞书应用鉴权响应已过期，请稍后重试")
        cache.tokens[key] = _Token(token, deadline)
        cache.tokens.move_to_end(key)
        while len(cache.tokens) > _MAX_CACHED_TOKENS:
            cache.tokens.popitem(last=False)
        return token


def _encode_payload(text: str, receiver: str, request_id: str) -> bytes:
    def encode(value: str) -> bytes:
        return json.dumps({
            "receive_id": receiver, "msg_type": "text",
            "content": json.dumps({"text": value}, ensure_ascii=False, separators=(",", ":")),
            "uuid": request_id,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    body = encode(text)
    if len(body) <= _MAX_REQUEST_BYTES:
        return body
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if len(encode(text[:mid] + _TRUNCATION_NOTICE)) <= _MAX_REQUEST_BYTES:
            low = mid
        else:
            high = mid - 1
    return encode(text[:low] + _TRUNCATION_NOTICE)


async def send_text(config: dict, title: str, content: str) -> None:
    valid = validate_config(config)
    request_id = str(uuid4())
    key = hashlib.sha256((valid["app_id"] + "\0" + valid["app_secret"]).encode()).hexdigest()
    cache = _cache_for_loop()
    for name in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
        logging.getLogger(name).addFilter(_HTTP_LOG_FILTER)
    context_token = _SENSITIVE_REQUEST.set(True)
    try:
        body = _encode_payload(f"{title}\n\n{content}" if title else content, valid["receive_id"], request_id)
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for attempt in range(2):
                token = await _get_token(client, valid, cache, key)
                response = await client.post(
                    _MESSAGE_URL, params={"receive_id_type": valid["receive_id_type"]}, content=body,
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=utf-8"},
                )
                try:
                    data = _read_response(response)
                except FeishuAppError as exc:
                    if exc.code in _INVALID_TOKEN_CODES:
                        cached = cache.tokens.get(key)
                        if cached and cached.value == token:
                            cache.tokens.pop(key, None)
                        if attempt == 0:
                            continue
                    raise
                message = data.get("data")
                if not isinstance(message, dict) or not isinstance(message.get("message_id"), str) or not message["message_id"].strip():
                    raise FeishuAppError("飞书应用消息响应缺少消息 ID，未能确认发送成功")
                return
    except FeishuAppError:
        raise
    except httpx.TimeoutException:
        raise FeishuAppError("飞书应用请求超时，请稍后检查接收结果") from None
    except httpx.HTTPError:
        raise FeishuAppError("飞书应用网络请求失败，请检查网络连接") from None
    except Exception:
        # 不让第三方异常携带 Authorization、请求正文或鉴权响应进入上层日志。
        raise FeishuAppError("飞书应用通知请求失败，请检查配置与连接") from None
    finally:
        _SENSITIVE_REQUEST.reset(context_token)
