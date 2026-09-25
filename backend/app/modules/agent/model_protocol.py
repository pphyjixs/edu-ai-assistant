"""Chat Completions 兼容端点的**单次**模型调用（开发方案 5.5）。

把"只调用一次模型"的部分从 :mod:`app.modules.agent.generation_ai` 下沉到这里，
使有界循环（:mod:`app.modules.agent.orchestration`）可以复用同一个传输层，
而最终答案的 JSON 解析与引用校验仍留在 ``generation_ai``。

刻意只做一件事：发请求、把响应收敛成 :class:`ModelCompletion`。它**不**决定
用哪套提示词，也**不**解释业务语义。

关键约定：

- 区分 ``assistant.tool_calls`` 与普通 ``assistant.content``；两者同时出现时
  由调用方决定（先执行工具，content 仅作中间文本，不展示、不落库）。
- 只对**连接建立失败**做有界重试——此时请求没送达模型，重试不会重复计费；
  读超时不重试。
- ``tools`` 不被支持时给出**明确**失败：``MODEL_TOOL_CALL_UNSUPPORTED``，
  而不是静默退回"让模型输出伪 tool JSON"。日志只写 provider/model 与状态码，
  不写提示词与课件原文。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("app.agent.model")

#: 连接建立失败的重试次数与退避（秒）
CONNECT_RETRY_ATTEMPTS = 3
CONNECT_RETRY_BACKOFF_SECONDS = (0.5, 1.5)

#: 出现这些状态码时，若响应体提到 tool/function，则判定 provider 不支持工具协议
_UNSUPPORTED_TOOL_STATUSES = frozenset({400, 404, 405, 422, 501})


class AgentModelNotConfiguredError(Exception):
    """模型端点或模型名称未配置（Worker 按失败处理，写安全摘要）。"""


class AgentGenerationError(Exception):
    """模型请求失败、超时或输出无效。消息可安全展示。"""


class ModelToolCallUnsupportedError(AgentGenerationError):
    """当前 ``AI_BASE_URL`` 对应的服务不支持 function calling（开发方案 5.5）。

    单独成类是为了把「不支持工具协议」与「模型暂时超时」区分成两种故障：
    前者需要换服务或调整配置，后者重试即可。
    """


@dataclass(frozen=True, slots=True)
class ToolCallDraft:
    """模型请求的一次工具调用（原始 JSON 字符串参数）。"""

    id: str
    name: str
    arguments: str


@dataclass(slots=True)
class ModelCompletion:
    """一次模型调用的结果。"""

    content: str | None
    tool_calls: list[ToolCallDraft] = field(default_factory=list)
    #: 本次请求实际使用的 model 名（写进 Run，便于回溯）
    model: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


def build_headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    return headers


def _post_with_connect_retry(
    client: httpx.Client, url: str, payload: dict[str, Any], headers: dict[str, str]
) -> httpx.Response:
    """POST 请求；只对**连接建立失败**做有界重试。"""
    last_error: Exception | None = None
    for attempt in range(CONNECT_RETRY_ATTEMPTS):
        try:
            return client.post(url, json=payload, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_error = exc
            logger.warning(
                "模型服务连接失败（第 %s/%s 次尝试）：%s",
                attempt + 1,
                CONNECT_RETRY_ATTEMPTS,
                type(exc).__name__,
            )
            if attempt < len(CONNECT_RETRY_BACKOFF_SECONDS):
                time.sleep(CONNECT_RETRY_BACKOFF_SECONDS[attempt])
        except httpx.TimeoutException as exc:
            raise AgentGenerationError("模型服务请求超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise AgentGenerationError(
                f"模型服务连接失败（{type(exc).__name__}），请稍后重试"
            ) from exc

    raise AgentGenerationError(
        f"模型服务连接失败（{type(last_error).__name__ if last_error else 'ConnectError'}），"
        f"已重试 {CONNECT_RETRY_ATTEMPTS} 次，请稍后重试"
    )


def _looks_like_unsupported_tools(response: httpx.Response) -> bool:
    """响应是否在说"我不认识 tools/function"（而不是普通的参数错误）。"""
    if response.status_code not in _UNSUPPORTED_TOOL_STATUSES:
        return False
    try:
        body = response.text.lower()
    except Exception:  # pragma: no cover - 响应体不可读时按普通错误处理
        return False
    return "tool" in body or "function" in body


def complete(
    *,
    client: httpx.Client,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = "auto",
    temperature: float = 0.2,
) -> ModelCompletion:
    """调用一次模型，返回内容与工具调用。

    :raises AgentModelNotConfiguredError: 未配置 ``base_url`` 或 ``model``。
    :raises ModelToolCallUnsupportedError: provider 明确不支持工具协议。
    :raises AgentGenerationError: 请求失败、超时或响应格式无效。
    """
    if not base_url.strip():
        raise AgentModelNotConfiguredError("Agent 未配置模型端点（AI_BASE_URL）")
    if not model.strip():
        raise AgentModelNotConfiguredError("Agent 未配置模型名称（AI_MODEL）")

    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

    response = _post_with_connect_retry(client, url, payload, build_headers(api_key))

    if _looks_like_unsupported_tools(response):
        # 日志只记录 provider/model 与状态码，不写提示词与请求体
        logger.warning(
            "模型服务不支持工具协议（provider=%s model=%s status=%s）",
            base_url,
            model,
            response.status_code,
        )
        raise ModelToolCallUnsupportedError(
            "当前模型服务不支持工具调用（function calling），无法执行站内操作"
        )
    if response.status_code in (401, 403):
        raise AgentGenerationError("模型服务拒绝了访问凭据")
    if response.status_code != 200:
        raise AgentGenerationError(f"模型服务返回非预期状态 {response.status_code}")

    try:
        body = response.json()
        message = body["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AgentGenerationError("模型服务响应格式无效") from exc

    raw_calls = message.get("tool_calls") if isinstance(message, dict) else None
    tool_calls: list[ToolCallDraft] = []
    if raw_calls:
        if not isinstance(raw_calls, list):
            raise AgentGenerationError("模型服务响应格式无效")
        for item in raw_calls:
            try:
                function = item["function"]
                call_id = item.get("id") or f"call_{len(tool_calls) + 1}"
                tool_calls.append(
                    ToolCallDraft(
                        id=str(call_id),
                        name=str(function["name"]),
                        arguments=function.get("arguments") or "",
                    )
                )
            except (KeyError, TypeError) as exc:
                raise AgentGenerationError("模型服务响应格式无效") from exc

    raw_content = message.get("content") if isinstance(message, dict) else None
    content = raw_content if isinstance(raw_content, str) else None

    if not tool_calls and not (content or "").strip():
        raise AgentGenerationError("模型服务响应格式无效")

    return ModelCompletion(content=content, tool_calls=tool_calls, model=model)


__all__ = [
    "CONNECT_RETRY_ATTEMPTS",
    "AgentGenerationError",
    "AgentModelNotConfiguredError",
    "ModelCompletion",
    "ModelToolCallUnsupportedError",
    "ToolCallDraft",
    "build_headers",
    "complete",
]
