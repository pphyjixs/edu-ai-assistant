"""回答生成的 AI 适配层（``docs/api-contract.md`` 6.1 / 6.5）。

通过**可替换的适配层**调用 Chat Completions 兼容端点（复用现有
``AI_BASE_URL`` / ``AI_MODEL`` / ``AI_API_KEY`` 配置）；单元与集成测试注入
``httpx.MockTransport`` 支撑的假模型服务，真实模型联调另行验收。

受约束的生成（契约 6.1）：

1. 提示词只包含**本次检索到的片段**（片段 ID、资料名、定位与原文）；
2. 模型输出必须是合法 JSON 且通过 :class:`GeneratedAnswer` 的 Pydantic 校验；
3. 服务端校验引用的**片段 ID 必须来自本次检索**，且 ``quote`` 必须能在该
   片段原文中找到（空白规范化后的子串匹配）——越界或对不上即丢弃该引用；
4. 丢弃后没有任何有效引用时按**无依据**处理：固定文案 + ``grounded=false``
   + 空引用，绝不输出模型编造的来源。

失败语义：模型端点/模型名未配置时抛 :class:`AnswerModelNotConfiguredError`
（服务层转 ``503 SERVICE_UNAVAILABLE``）；请求超时、HTTP 错误、无效输出抛
:class:`AnswerGenerationError`（服务层转 ``502 AI_JOB_FAILED``，不落库消息）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from app.modules.chat.retrieval import RetrievedChunk
from app.modules.chat.schemas import (
    NO_EVIDENCE_ANSWER,
    GeneratedAnswer,
)

logger = logging.getLogger("app.chat.ai")

#: 提示词版本：写入生成尝试记录，便于回溯是哪个版本产生的回答
PROMPT_VERSION = "chat-answer-v1"

SYSTEM_PROMPT = (
    "你是课程助教。你只能依据用户提供的课件片段回答问题，"
    "不得使用片段之外的知识，也不得编造来源。"
)

_USER_PROMPT_TEMPLATE = """下面是本次检索到的课件片段（只能引用这些片段）：

{chunks}

用户问题：{question}

请严格按以下要求回答：
1. 只依据上面的片段作答；片段中没有的信息不要出现在回答里；
2. 用与问题相同的语言作答；
3. 如果片段不足以回答，把 citations 留空数组；
4. citations 里每一项的 chunk_id 必须是上面出现过的片段 ID；
   quote 必须逐字摘自该片段原文，不得改写或概括；
5. 只输出 JSON 对象，不要输出任何其他文字或代码块围栏。
JSON 格式：
{{"answer": "...", "citations": [{{"chunk_id": "片段 ID", "quote": "原文摘录"}}]}}"""


class AnswerModelNotConfiguredError(Exception):
    """模型端点或模型名称未配置（服务层转 503）。"""


class AnswerGenerationError(Exception):
    """模型请求失败或输出无效（服务层转 502）。消息可安全展示。"""


@dataclass(frozen=True, slots=True)
class ValidatedCitation:
    """通过服务端校验的引用：片段 + 可核对摘录。"""

    chunk: RetrievedChunk
    quote: str


@dataclass(frozen=True, slots=True)
class ValidatedAnswer:
    """服务端校验后的回答。"""

    content: str
    grounded: bool
    citations: list[ValidatedCitation]


def _normalize(text: str) -> str:
    """摘录核对用规范化：压缩全部空白（含换行），保证跨行摘录可命中。"""
    return re.sub(r"\s+", "", text)


def _truncate_quote(quote: str, *, limit: int = 300) -> str:
    """限制展示用摘录长度，避免把整段原文塞进引用。"""
    return quote if len(quote) <= limit else quote[:limit]


def build_prompt(question: str, chunks: list[RetrievedChunk]) -> str:
    """构造用户提示：只包含本次检索到的片段。"""
    blocks = []
    for index, chunk in enumerate(chunks, start=1):
        location = f"{chunk.location_start}-{chunk.location_end}"
        blocks.append(
            f"[片段 {index}] chunk_id={chunk.chunk_id}\n"
            f"资料：{chunk.material_name}（位置 {location}）\n"
            f"原文：\n{chunk.content}"
        )
    return _USER_PROMPT_TEMPLATE.format(
        chunks="\n\n".join(blocks), question=question
    )


def _extract_json(content: str) -> dict:
    """从模型回复中提取 JSON 对象；容忍代码块围栏。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnswerGenerationError("模型输出不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise AnswerGenerationError("模型输出不是 JSON 对象")
    return parsed


def validate_answer(
    answer: GeneratedAnswer, chunks: list[RetrievedChunk]
) -> ValidatedAnswer:
    """服务端校验引用，并决定回答是否有依据（契约 6.1 / 6.6）。"""
    by_id = {str(chunk.chunk_id).lower(): chunk for chunk in chunks}
    validated: list[ValidatedCitation] = []
    seen: set[str] = set()

    for citation in answer.citations:
        chunk = by_id.get(citation.chunk_id.strip().lower())
        if chunk is None:
            logger.warning("模型引用了本次检索之外的片段，已丢弃该引用")
            continue
        normalized_quote = _normalize(citation.quote)
        if not normalized_quote or normalized_quote not in _normalize(chunk.content):
            logger.warning("模型摘录无法在片段原文中找到，已丢弃该引用")
            continue
        if chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        validated.append(
            ValidatedCitation(chunk=chunk, quote=_truncate_quote(citation.quote.strip()))
        )

    if not validated:
        return ValidatedAnswer(
            content=NO_EVIDENCE_ANSWER, grounded=False, citations=[]
        )
    return ValidatedAnswer(
        content=answer.answer.strip(), grounded=True, citations=validated
    )


def generate_answer(
    chunks: list[RetrievedChunk],
    *,
    question: str,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> ValidatedAnswer:
    """调用模型生成回答并完成服务端校验（同步，服务层在线程池中执行）。

    :raises AnswerModelNotConfiguredError: 未配置模型端点或模型名称。
    :raises AnswerGenerationError: 请求失败、超时或输出无效。
    """
    if not base_url.strip():
        raise AnswerModelNotConfiguredError("问答服务未配置模型端点（AI_BASE_URL）")
    if not model.strip():
        raise AnswerModelNotConfiguredError("问答服务未配置模型名称（AI_MODEL）")
    if not chunks:
        # 没有可引用的片段时不调用模型：直接按无依据处理（契约 6.1）
        return ValidatedAnswer(content=NO_EVIDENCE_ANSWER, grounded=False, citations=[])

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(question, chunks)},
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
        try:
            response = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise AnswerGenerationError("模型服务请求超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise AnswerGenerationError(
                f"模型服务连接失败（{type(exc).__name__}），请稍后重试"
            ) from exc

        if response.status_code in (401, 403):
            raise AnswerGenerationError("模型服务拒绝了访问凭据")
        if response.status_code != 200:
            raise AnswerGenerationError(
                f"模型服务返回非预期状态 {response.status_code}"
            )

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AnswerGenerationError("模型服务响应格式无效") from exc

        try:
            answer = GeneratedAnswer.model_validate(_extract_json(content))
        except ValidationError as exc:
            raise AnswerGenerationError("模型输出未通过结构校验") from exc

        return validate_answer(answer, chunks)
    finally:
        if owned:
            client.close()
