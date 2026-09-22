"""Agent 的模型适配层（``docs/local-development-agent-backend.md`` 第 6.7 节）。

复用现有的 Chat Completions 兼容端点配置（``AI_BASE_URL`` / ``AI_MODEL`` /
``AI_API_KEY``），但使用 **Agent 自己的超时**（``AGENT_MODEL_TIMEOUT_SECONDS``），
因为一次上下文总结可能耗时数分钟，而同步问答的 60 秒显然不够。

受约束的生成（与 chat 的回答校验同源）：

1. 提示词只包含本次解析出的来源块与业务对象摘要；
2. 模型输出必须是合法 JSON 且通过 :class:`GeneratedAgentAnswer` 校验；
3. 服务端校验引用的 **ref 必须是本次注入的编号**，``quote`` 必须能在该块文本中
   找到（空白规范化后子串匹配）；越界或对不上即丢弃该引用；
4. 只有落在**资料**上的来源可以成为用户可见引用——消息引用表指向 ``materials``，
   作业与课程摘要虽然注入了上下文，但不进引用列表；
5. 没有任何有效引用时按**无依据**处理：固定文案 + ``grounded=false`` + 空引用。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from app.modules.agent.context import ContextBlock, ResolvedContext
from app.modules.agent.models import AgentRunAction
from app.modules.agent.prompts import build_system_prompt, build_user_prompt
from app.modules.agent.schemas import GeneratedAgentAnswer
from app.modules.chat.schemas import NO_EVIDENCE_ANSWER

logger = logging.getLogger("app.agent.ai")

#: 单条摘录入库的长度上限
QUOTE_MAX_LENGTH = 300

#: 连接建立失败的重试次数与退避（秒）。
#: 只重试**连接建立**阶段的失败——此时请求还没有送达模型，重试不会产生
#: 重复计费，也不会有"半次生成"的歧义；读超时（模型可能已开始生成）不重试。
CONNECT_RETRY_ATTEMPTS = 3
CONNECT_RETRY_BACKOFF_SECONDS = (0.5, 1.5)


class AgentModelNotConfiguredError(Exception):
    """模型端点或模型名称未配置（Worker 按失败处理，写安全摘要）。"""


class AgentGenerationError(Exception):
    """模型请求失败、超时或输出无效。消息可安全展示。"""


@dataclass(frozen=True, slots=True)
class ValidatedAgentCitation:
    """通过校验的引用：来源块 + 可核对摘录。"""

    block: ContextBlock
    quote: str


@dataclass(frozen=True, slots=True)
class ValidatedAgentAnswer:
    """服务端校验后的回答。"""

    content: str
    grounded: bool
    citations: list[ValidatedAgentCitation]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _extract_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentGenerationError("模型输出不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise AgentGenerationError("模型输出不是 JSON 对象")
    return parsed


def validate_answer(
    answer: GeneratedAgentAnswer, context: ResolvedContext
) -> ValidatedAgentAnswer:
    """按本次注入的来源校验引用（文档 6.7）。"""
    by_ref = context.block_by_ref()
    validated: list[ValidatedAgentCitation] = []
    seen: set[str] = set()

    for citation in answer.citations:
        ref = citation.ref.strip().upper()
        block = by_ref.get(ref)
        if block is None:
            logger.warning("模型引用了本次上下文之外的编号 %s，已丢弃", ref)
            continue
        normalized_quote = _normalize(citation.quote)
        if not normalized_quote or normalized_quote not in _normalize(block.text):
            logger.warning("模型摘录无法在来源 %s 的原文中找到，已丢弃", ref)
            continue
        # 作业/课程摘要注入模型但不对用户展示为引用（引用表指向资料）
        if not block.citable:
            logger.info("来源 %s 不是资料，仅作为上下文，不生成用户可见引用", ref)
            continue
        if ref in seen:
            continue
        seen.add(ref)
        quote = citation.quote.strip()
        validated.append(
            ValidatedAgentCitation(
                block=block,
                quote=quote if len(quote) <= QUOTE_MAX_LENGTH else quote[:QUOTE_MAX_LENGTH],
            )
        )

    if not validated:
        return ValidatedAgentAnswer(
            content=NO_EVIDENCE_ANSWER, grounded=False, citations=[]
        )
    return ValidatedAgentAnswer(
        content=answer.answer.strip(), grounded=True, citations=validated
    )


def _post_with_connect_retry(
    client: httpx.Client, url: str, payload: dict, headers: dict
) -> httpx.Response | None:
    """POST 请求；只对**连接建立失败**做有界重试。

    - 连接建立失败（``ConnectError`` / ``ConnectTimeout``）说明请求没送达模型，
      重试安全且大概率能成功；
    - 读超时说明模型可能已经开始生成，重试会重复消耗额度，因此直接上抛；
    - 其他 HTTP 错误原样上抛。
    """
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


def generate_answer(
    context: ResolvedContext,
    *,
    action: AgentRunAction,
    question: str,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> ValidatedAgentAnswer:
    """调用模型生成回答并完成服务端校验（同步，Worker 在线程池中执行）。

    :raises AgentModelNotConfiguredError: 未配置模型端点或模型名称。
    :raises AgentGenerationError: 请求失败、超时或输出无效。
    """
    if not context.blocks:
        # 没有任何可引用内容：不调用模型，也不要求模型配置（与 chat 一致）
        return ValidatedAgentAnswer(
            content=NO_EVIDENCE_ANSWER, grounded=False, citations=[]
        )
    if not base_url.strip():
        raise AgentModelNotConfiguredError("Agent 未配置模型端点（AI_BASE_URL）")
    if not model.strip():
        raise AgentModelNotConfiguredError("Agent 未配置模型名称（AI_MODEL）")

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": build_system_prompt(action)},
            {"role": "user", "content": build_user_prompt(context, question=question)},
        ],
        "temperature": 0.2,
    }
    headers = {"Content-Type": "application/json"}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    owned = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(timeout_seconds))

    try:
        response = _post_with_connect_retry(client, url, payload, headers)

        if response.status_code in (401, 403):
            raise AgentGenerationError("模型服务拒绝了访问凭据")
        if response.status_code != 200:
            raise AgentGenerationError(f"模型服务返回非预期状态 {response.status_code}")

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AgentGenerationError("模型服务响应格式无效") from exc

        try:
            answer = GeneratedAgentAnswer.model_validate(_extract_json(content))
        except ValidationError as exc:
            raise AgentGenerationError("模型输出未通过结构校验") from exc

        return validate_answer(answer, context)
    finally:
        if owned:
            client.close()
