"""TypeSafe System One 传输适配。业务问题由 research 模块定义。"""
from __future__ import annotations

import asyncio
import json
import math

import httpx


class JevError(Exception):
    """可以展示给用户的错误；不包含请求、密钥或上游响应正文。"""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _probability(value: object) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and 0 <= value <= 1
    )


def _validate_response(payload: object, questions: dict) -> dict:
    error = JevError("Jev 返回的数据格式或概率无效，请稍后重试。")
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
        raise error
    if not payload["model"].strip():
        raise error
    answers = payload.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise error
    for name, question in questions.items():
        answer = answers[name]
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            raise error
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != set(question["criteria"]):
            raise error
        if not all(_probability(value) for value in probabilities.values()):
            raise error
        if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=0.001):
            raise error
        if not _probability(answer.get("confidence")):
            raise error
        choice = answer.get("choice")
        if not isinstance(choice, str) or choice not in probabilities:
            raise error
        if probabilities[choice] + 1e-9 < max(probabilities.values()):
            raise error
    # Return the documented fields only, rather than arbitrary provider extras.
    return {
        "model": payload["model"],
        "answers": {
            name: {key: answer[key] for key in ("type", "choice", "probabilities", "confidence")}
            for name, answer in answers.items()
        },
        "usage": payload.get("usage", {}) if isinstance(payload.get("usage", {}), dict) else {},
    }


class JevClient:
    """异步 Choice 接口；固定官方端点，最多重试一次明确的限流响应。"""

    def __init__(
        self, api_key: str, model: str = "jev-latest", timeout: float = 30, proxy: str = "",
    ):
        self._api_key = api_key.strip()
        self.model = model
        self.timeout = timeout
        self.proxy = proxy

    async def evaluate(self, state: dict, questions: dict) -> dict:
        if not self._api_key:
            raise JevError("尚未配置 Jev，请在服务端设置 TYPESAFE_API_KEY 后重启。", 503)
        if not questions or any(
            not isinstance(question, dict)
            or question.get("type") != "choice"
            or not question.get("instructions")
            or not isinstance(question.get("criteria"), dict)
            or not 2 <= len(question["criteria"]) <= 255
            for question in questions.values()
        ):
            raise JevError("Jev 判断问题配置无效。", 422)
        body = {"model": self.model, "state": state, "questions": questions}
        try:
            json.dumps(body, allow_nan=False)
        except (TypeError, ValueError):
            raise JevError("Jev 输入包含无效数据，未发送请求。", 422) from None

        options = {"timeout": httpx.Timeout(self.timeout), "follow_redirects": False}
        if self.proxy:
            options["proxy"] = self.proxy
        try:
            async with httpx.AsyncClient(**options) as client:
                for attempt in range(2):
                    response = await client.post(
                        "https://api.typesafe.ai/v1/systemone",
                        headers={"Authorization": f"Bearer {self._api_key}"}, json=body,
                    )
                    if response.status_code not in (429, 529) or attempt == 1:
                        break
                    try:
                        delay = float(response.headers.get("Retry-After", "1"))
                    except ValueError:
                        delay = 1.0
                    await asyncio.sleep(min(5.0, max(0.0, delay)) if math.isfinite(delay) else 1.0)
        except httpx.TimeoutException:
            # An ambiguous timeout may already have been billed. Do not retry it.
            raise JevError("Jev 请求超时，请稍后手动重试。", 504) from None
        except httpx.RequestError:
            raise JevError("无法连接 Jev 服务，请检查服务器网络或代理。") from None

        if response.status_code in (401, 403):
            raise JevError("Jev 密钥无效或账户没有访问权限，请检查服务端配置。", 503)
        if response.status_code in (429, 529):
            raise JevError("Jev 服务限流或繁忙，请稍后重试。", 503)
        if response.status_code != 200:
            raise JevError(f"Jev 服务返回错误（HTTP {response.status_code}）。")
        try:
            payload = response.json()
        except ValueError:
            raise JevError("Jev 返回了无法解析的数据。") from None
        return _validate_response(payload, questions)
