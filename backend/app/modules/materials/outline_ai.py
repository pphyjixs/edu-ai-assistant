"""模型大纲生成：调用 Chat Completions 兼容接口生成章节与知识点（契约 5.5 第 2 步）。

与 :mod:`app.modules.materials.extraction` 的分工：提取层产出**带来源位置的
文本块**，本模块把分块文本送入可配置的 Chat Completions 兼容端点
（OpenAI API 形状），生成章节标题与知识点，并对输出做严格校验：

- 输出必须是合法 JSON 且通过 :class:`GeneratedOutline` 的 Pydantic 校验；
- 每条 ``quote`` 必须能在对应来源文本中找到（空白规范化后子串匹配），
  找不到视为模型幻觉、整体无效；
- 位置必须落在材料的实际来源范围内。

任何失败（超时、HTTP 错误、无效输出、幻觉摘录）都抛
:class:`OutlineGenerationError`，由 Worker 转成任务与资料的 ``FAILED``
并写入安全摘要——不会发布部分结果（契约 5.5 第 4 步）。

测试用本地假模型 HTTP 服务（``httpx.MockTransport`` 或本地 HTTP 服务）
替换真实端点；**真实外部模型调用未做验收**。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.materials.extraction import (
    TextChunk,
    QuoteNotInSourceError,
    verify_quote_in_source,
)

logger = logging.getLogger("app.materials.ai")

SYSTEM_PROMPT = (
    "你是课件解析助手。你会收到按来源顺序分块的课件全文，"
    "每个块带有来源位置标记（页码/幻灯片号/段落号，从 1 开始）。"
    "请阅读全文并输出结构化学习大纲。"
)

USER_PROMPT_TEMPLATE = """{chunks}

请按以下要求输出：
1. 判断全文的主要语言，所有标题、说明和摘录都使用同一种语言；
2. 按来源顺序划分章节；每个章节包含 title（章节标题）、
   location_start 与 location_end（该章节覆盖的来源位置范围）、
   knowledge_points（该章节下的知识点列表）；
3. 每个知识点包含 title（知识点标题）、description（简短说明）、
   quote（可核对的原文摘录，必须逐字取自原文，不得改写或概括）、
   location_start 与 location_end（该知识点的来源位置）；
4. 位置均为从 1 开始的整数；
5. 只输出 JSON 对象，不要输出任何其他文字或代码块围栏。
JSON 格式：
{{"sections": [{{"title": "...", "location_start": 1, "location_end": 2,
"knowledge_points": [{{"title": "...", "description": "...", "quote": "...",
"location_start": 1, "location_end": 1}}]}}]}}"""


class GeneratedKnowledgePoint(BaseModel):
    """模型输出的单个知识点（发布前转成 ORM 记录）。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2000)
    quote: str = Field(min_length=1, max_length=2000)
    location_start: int = Field(ge=1)
    location_end: int = Field(ge=1)

    @field_validator("location_end")
    @classmethod
    def _end_not_before_start(cls, value: int, info) -> int:  # noqa: ANN001
        start = info.data.get("location_start")
        if start is not None and value < start:
            raise ValueError("location_end 不能小于 location_start")
        return value


class GeneratedSection(BaseModel):
    """模型输出的单个章节。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    location_start: int = Field(ge=1)
    location_end: int = Field(ge=1)
    knowledge_points: list[GeneratedKnowledgePoint] = Field(min_length=1, max_length=50)

    @field_validator("location_end")
    @classmethod
    def _end_not_before_start(cls, value: int, info) -> int:  # noqa: ANN001
        start = info.data.get("location_start")
        if start is not None and value < start:
            raise ValueError("location_end 不能小于 location_start")
        return value


class GeneratedOutline(BaseModel):
    """模型输出的完整大纲（Pydantic 校验的形状契约）。"""

    model_config = ConfigDict(extra="forbid")

    sections: list[GeneratedSection] = Field(min_length=1, max_length=200)


class OutlineGenerationError(Exception):
    """大纲生成失败：超时、HTTP 错误、无效输出或幻觉摘录。

    消息可安全展示（写入任务与资料的 FAILED 说明）。
    """


def _build_prompt(chunks: list[TextChunk]) -> str:
    return USER_PROMPT_TEMPLATE.format(
        chunks="\n\n".join(chunk.text for chunk in chunks)
    )


def _extract_json(content: str) -> dict[str, Any]:
    """从模型回复中提取 JSON 对象；容忍代码块围栏。"""
    text = content.strip()
    if text.startswith("```"):
        # 剥掉 ```json ... ``` 围栏
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OutlineGenerationError("模型输出不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise OutlineGenerationError("模型输出不是 JSON 对象")
    return parsed


def _validate_locations_and_quotes(
    outline: GeneratedOutline, chunks: list[TextChunk]
) -> None:
    """校验位置范围与原文摘录（契约 5.5：摘录必须能在来源文本中找到）。"""
    if not chunks:  # pragma: no cover - 提取层保证非空
        raise OutlineGenerationError("没有可用的来源文本")
    max_location = max(chunk.location_end for chunk in chunks)
    body_pool = "\n".join(chunk.body for chunk in chunks)

    try:
        for section in outline.sections:
            if section.location_end > max_location:
                raise OutlineGenerationError(
                    f"章节「{section.title}」的位置超出材料范围"
                )
            for point in section.knowledge_points:
                if point.location_end > max_location:
                    raise OutlineGenerationError(
                        f"知识点「{point.title}」的位置超出材料范围"
                    )
                verify_quote_in_source(point.quote, body_pool)
    except QuoteNotInSourceError as exc:
        raise OutlineGenerationError(
            f"模型输出的原文摘录无效：{exc}，已拒绝本次解析结果"
        ) from exc


def generate_outline(
    chunks: list[TextChunk],
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> GeneratedOutline:
    """调用 Chat Completions 兼容端点生成大纲（同步，Worker 内执行）。

    :param client: 可注入的 ``httpx.Client``（测试用假模型服务替换）；
        未提供时按超时与鉴权头构造一次性客户端。

    :raises OutlineGenerationError: 模型未配置、请求失败或输出无效。
    """
    if not base_url.strip():
        raise OutlineGenerationError(
            "解析服务未配置模型端点（AI_BASE_URL），无法生成大纲"
        )
    if not model.strip():
        raise OutlineGenerationError("解析服务未配置模型名称（AI_MODEL）")

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(chunks)},
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
            raise OutlineGenerationError("模型服务请求超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise OutlineGenerationError(
                f"模型服务连接失败（{type(exc).__name__}），请稍后重试"
            ) from exc

        if response.status_code in (401, 403):
            raise OutlineGenerationError("模型服务拒绝了访问凭据")
        if response.status_code != 200:
            raise OutlineGenerationError(
                f"模型服务返回非预期状态 {response.status_code}"
            )

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise OutlineGenerationError("模型服务响应格式无效") from exc

        outline = GeneratedOutline.model_validate(_extract_json(content))
        _validate_locations_and_quotes(outline, chunks)
        return outline
    finally:
        if owned:
            client.close()
