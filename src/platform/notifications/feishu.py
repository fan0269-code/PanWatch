"""国内飞书自定义机器人传输；不复用国际 Lark 的域名。

协议：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
签名以 ``timestamp + "\\n" + secret`` 为 HMAC-SHA256 key，对空消息签名。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time

import httpx

_WEBHOOK_PATTERN = re.compile(r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9_-]{1,128}")
_MAX_REQUEST_BYTES = 20_000
_TRUNCATION_NOTICE = "\n\n…内容过长已截断，完整报告请在 PanWatch 查看。"


def validate_config(config: dict) -> dict[str, str]:
    """只允许国内官方 Webhook，错误文本不包含用户提交的凭据。"""
    if not isinstance(config, dict):
        raise ValueError("飞书渠道配置必须为对象")
    url = config.get("webhook_url")
    if not isinstance(url, str) or not _WEBHOOK_PATTERN.fullmatch(url.strip()):
        raise ValueError("飞书需要完整的官方 HTTPS Webhook URL，且不能含端口、查询参数或尾斜杠")
    secret = config.get("secret", "")
    if secret is None:
        secret = ""
    if not isinstance(secret, str) or len(secret.strip()) > 256:
        raise ValueError("飞书签名密钥必须为不超过 256 字符的字符串")
    return {"webhook_url": url.strip(), "secret": secret.strip()}


def sign_request(timestamp: int, secret: str) -> str:
    key = f"{timestamp}\n{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode("ascii")


def _encode_payload(text: str, secret: str) -> bytes:
    payload = {"msg_type": "text", "content": {"text": text}}
    if secret:
        timestamp = int(time.time())
        payload.update(timestamp=str(timestamp), sign=sign_request(timestamp, secret))

    def encode(value: str) -> bytes:
        payload["content"]["text"] = value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    body = encode(text)
    if len(body) <= _MAX_REQUEST_BYTES:
        return body
    # 按实际 JSON UTF-8 字节数限制，避免中文/表情/转义字符越界。
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if len(encode(text[:mid] + _TRUNCATION_NOTICE)) <= _MAX_REQUEST_BYTES:
            low = mid
        else:
            high = mid - 1
    return encode(text[:low] + _TRUNCATION_NOTICE)


class _RedactWebhookLog(logging.Filter):
    """httpx INFO 日志包含完整 URL，必须在其传播到持久化日志前脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "open.feishu.cn/open-apis/bot/v2/hook/" in message:
            record.msg = re.sub(
                r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[^\s\"'<>]+",
                "https://open.feishu.cn/open-apis/bot/v2/hook/[REDACTED]",
                message,
            )
            record.args = ()
        return True


_LOG_REDACTOR = _RedactWebhookLog()


async def send_text(config: dict, title: str, content: str) -> None:
    """发送已经转成纯文本的标题/正文，只有 HTTP 和业务码均成功才返回。"""
    valid = validate_config(config)
    text = f"{title}\n\n{content}" if title else content
    body = _encode_payload(text, valid["secret"])
    for name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
        logging.getLogger(name).addFilter(_LOG_REDACTOR)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.post(
                valid["webhook_url"], content=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
    except httpx.TimeoutException:
        raise RuntimeError("飞书请求超时，请检查网络连接") from None
    except httpx.HTTPError:
        raise RuntimeError("飞书网络请求失败，请检查连接和代理配置") from None
    if response.status_code != 200:
        raise RuntimeError(f"飞书服务返回 HTTP {response.status_code}")
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        raise RuntimeError("飞书服务返回了无效 JSON") from None
    if not isinstance(data, dict):
        raise RuntimeError("飞书服务响应格式无效")
    # 当前 v2 为 code；兼容旧成功响应的 StatusCode，缺失或非整数不算成功。
    codes = [data[key] for key in ("code", "StatusCode") if key in data]
    if not codes or any(type(code) is not int for code in codes):
        raise RuntimeError("飞书服务响应缺少有效业务状态码")
    for code in codes:
        if code != 0:
            hint = {
                19021: "签名校验失败，请检查签名密钥和服务器时间",
                19022: "IP 白名单校验失败",
                19024: "关键词校验失败，请检查机器人安全设置",
                11232: "请求过于频繁，请稍后重试",
            }.get(code, "请检查机器人 Webhook 和安全设置")
            raise RuntimeError(f"飞书业务错误 {code}：{hint}")
