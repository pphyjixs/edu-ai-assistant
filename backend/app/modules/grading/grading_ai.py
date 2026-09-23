"""报告批改的 AI 适配层与服务端输出校验（``docs/api-contract.md`` 9.11）。

可替换的适配层：生产环境调用 Chat Completions 兼容端点（复用
``AI_BASE_URL`` / ``AI_MODEL`` / ``AI_API_KEY`` / ``AI_TIMEOUT_SECONDS``），
测试注入 ``httpx.MockTransport`` 支撑的假模型服务。

服务端职责（模型只负责"打分"，配额、位置与证据由服务端兜住）：

1. 模型必须对**每一个**评分项**恰好返回一项**：缺失、重复或返回本次版本之外的
   评分项一律整次失败；
2. 建议分必须在 ``0..max_score`` 之间、最多两位小数（``Decimal`` 判定）；
3. 每条判断说明与证据摘录都不得为空；
4. 每条结果必须给出 ``location_start`` / ``location_end``：严格整数、从 1 开始、
   不倒序，且**两个端点**都必须是报告实际提取到的单元（区间中间允许空页/空段落）；
5. **证据摘录必须出现在模型声明的位置区间内**（空白规范化后子串匹配）——
   只在报告其他位置出现不算数，否则视为幻觉，整次失败；
6. ``evidence_source_type`` 由报告 MIME 在服务端确定（PDF 按页、DOCX 按段落），
   **模型不能自行决定**；
7. 任何一项不满足即整次失败（``FAILED``），**不写部分批改结果**。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from app.modules.grading.models import (
    EVIDENCE_SOURCE_DOCX_PARAGRAPH,
    EVIDENCE_SOURCE_PDF_PAGE,
    SCORE_SCALE,
)

logger = logging.getLogger("app.grading.ai")

#: 提示词版本：写入批改尝试记录
PROMPT_VERSION = "submission-grade-v2"

SYSTEM_PROMPT = (
    "你是实验报告批改助手。你只能依据用户提供的报告原文评分，"
    "不得引入原文之外的信息，也不得编造证据或位置。"
)

_USER_PROMPT_TEMPLATE = """请依据下面的实验报告原文，按评分规则逐项打分。

评分规则（必须对每一项恰好给出一条结果，rubric_item_id 必须逐字照抄）：
{rubric}

实验报告原文（每行开头标注来源位置——{source_label}号，引用证据时必须给出该位置）：
{report}

要求：
1. 只依据报告原文评分，不得引入原文之外的信息；
2. 每条结果给出：score（建议分，0 到该项满分之间，最多两位小数）、
   comment（判断说明）、evidence_quote（逐字摘自报告原文的证据摘录，
   必须是原文中真实出现的连续片段）、location_start 与 location_end
   （证据所在的{source_label}号区间，必须与上面标注的位置一致）、
   error_type（错误类型，可为空字符串）、improvement_suggestion（改进建议）；
3. 每条结果都要引用真实存在的原文片段与真实位置，不要改写、概括或猜测位置；
4. 只输出 JSON 对象，不要输出其他文字或代码块围栏；
JSON 格式：
{{"summary": "整体评语", "items": [{{"rubric_item_id": "评分项 ID", "score": 35, "comment": "判断说明", "evidence_quote": "原文证据", "location_start": 1, "location_end": 1, "error_type": "需求遗漏", "improvement_suggestion": "改进建议"}}]}}"""

#: 位置标注的展示名
SOURCE_LABELS: dict[str, str] = {
    EVIDENCE_SOURCE_PDF_PAGE: "页",
    EVIDENCE_SOURCE_DOCX_PARAGRAPH: "段落",
}


class GradeModelNotConfiguredError(Exception):
    """模型端点或模型名称未配置（Worker 按失败处理并给出安全摘要）。"""


class GradeGenerationError(Exception):
    """模型请求失败或输出非法（消息可安全展示）。"""


@dataclass(frozen=True, slots=True)
class RubricItemSnapshot:
    """批改时**固定**的评分项快照（来自提交引用的评分版本）。"""

    rubric_item_id: uuid.UUID
    order: int
    title: str
    description: str
    max_score: Decimal


@dataclass(frozen=True, slots=True)
class ReportSource:
    """报告原文的提取结果：来源类型 + 带位置的文本单元（契约 9.11）。

    :ivar source_type: ``PDF_PAGE`` 或 ``DOCX_PARAGRAPH``（服务端按 MIME 确定）
    :ivar units: ``(位置, 文本)`` 序列，位置从 1 开始、按来源顺序排列
    """

    source_type: str
    units: list[tuple[int, str]]

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source_type, "位置")

    def locations(self) -> set[int]:
        """实际提取到的位置集合。"""
        return {location for location, _ in self.units}

    def text_within(self, start: int, end: int) -> str:
        """区间内（含边界）的原文拼接结果。"""
        return "\n".join(
            text for location, text in self.units if start <= location <= end
        )


@dataclass(frozen=True, slots=True)
class ValidatedGradeItem:
    """通过服务端校验的单个评分项结果（含证据定位）。"""

    rubric_item_id: uuid.UUID
    order: int
    score: Decimal
    comment: str
    evidence_quote: str
    evidence_source_type: str
    evidence_location_start: int
    evidence_location_end: int
    error_type: str
    improvement_suggestion: str


@dataclass(frozen=True, slots=True)
class ValidatedGrade:
    """整份批改结果。

    :ivar raw_output: 模型返回的原始结构化输出（审计用，落库前截断）
    """

    summary: str
    items: list[ValidatedGradeItem]
    raw_output: dict | None = None


class GeneratedGradeItem(BaseModel):
    """模型输出的单个评分项（结构校验；业务规则在 :func:`validate_generated`）。"""

    model_config = ConfigDict(extra="ignore")

    rubric_item_id: StrictStr
    score: float
    comment: StrictStr
    evidence_quote: StrictStr
    #: 严格整数：布尔与字符串在这里就被拒绝；零/负数/倒序由服务端校验给出稳定原因
    location_start: StrictInt
    location_end: StrictInt
    error_type: StrictStr = ""
    improvement_suggestion: StrictStr = ""


class GeneratedGrade(BaseModel):
    """模型输出的整份批改（结构校验）。"""

    model_config = ConfigDict(extra="ignore")

    summary: StrictStr = Field(min_length=1)
    items: list[GeneratedGradeItem] = Field(min_length=1)


def _normalize_for_match(text: str) -> str:
    """证据核对用规范化：压缩全部空白（含换行），保证跨行摘录可命中。"""
    return re.sub(r"\s+", "", text)


def _to_decimal(value: object) -> Decimal:
    """把模型给出的分数转成 ``Decimal``（拒绝布尔、NaN 与无穷值）。"""
    if isinstance(value, bool):
        raise GradeGenerationError("模型给出的分数必须是数值")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise GradeGenerationError("模型给出的分数不是合法数值") from exc
    if not decimal.is_finite():
        raise GradeGenerationError("模型给出的分数不是有限值")
    return decimal


def _validate_location(
    item: GeneratedGradeItem, *, report: ReportSource
) -> tuple[int, int]:
    """校验模型声明的证据位置区间（契约 9.11）。

    两个端点都必须是报告**实际提取到的**单元（页/段落）；区间中间允许
    存在没有文本的空页或空段落，但模型不能虚报实际不存在的端点。

    :raises GradeGenerationError: 零、负数、倒序或任一端点不存在。
    """
    start, end = item.location_start, item.location_end
    if start < 1 or end < 1:
        raise GradeGenerationError("模型给出的证据位置必须从 1 开始")
    if end < start:
        raise GradeGenerationError("模型给出的证据位置区间倒序")
    locations = report.locations()
    if start not in locations or end not in locations:
        raise GradeGenerationError(
            f"模型给出的证据位置（{start}–{end}）不在报告实际提取范围内"
        )
    return start, end


def validate_generated(
    generated: GeneratedGrade,
    *,
    rubric: list[RubricItemSnapshot],
    report: ReportSource,
) -> ValidatedGrade:
    """校验模型输出并生成服务端的批改快照。

    :raises GradeGenerationError: 任一项不符合要求（整次批改失败）。
    """
    expected = {item.rubric_item_id: item for item in rubric}
    if len(generated.items) != len(expected):
        raise GradeGenerationError("模型给出的评分项数量与评分规则不一致")

    seen: set[uuid.UUID] = set()
    validated: list[ValidatedGradeItem] = []

    for raw in generated.items:
        try:
            item_id = uuid.UUID(raw.rubric_item_id.strip())
        except (ValueError, AttributeError) as exc:
            raise GradeGenerationError("模型给出的 rubric_item_id 不是合法 UUID") from exc
        snapshot = expected.get(item_id)
        if snapshot is None:
            raise GradeGenerationError("模型给出了评分规则之外的评分项")
        if item_id in seen:
            raise GradeGenerationError("模型对同一评分项给出了多条结果")
        seen.add(item_id)

        score = _to_decimal(raw.score)
        if score < 0 or score > snapshot.max_score:
            raise GradeGenerationError("模型给出的建议分超出了该评分项的满分范围")
        if -score.as_tuple().exponent > SCORE_SCALE:
            raise GradeGenerationError("模型给出的建议分小数位超过两位")

        start, end = _validate_location(raw, report=report)
        quote = raw.evidence_quote.strip()
        # 证据摘录必须出现在**声明的位置区间内**，不能只在报告其他位置出现
        if not quote or _normalize_for_match(quote) not in _normalize_for_match(
            report.text_within(start, end)
        ):
            raise GradeGenerationError(
                f"模型给出的证据摘录无法在声明的位置（{start}–{end}）中找到"
            )

        comment = raw.comment.strip()
        if not comment:
            raise GradeGenerationError("模型给出的判断说明为空")

        validated.append(
            ValidatedGradeItem(
                rubric_item_id=item_id,
                order=snapshot.order,
                score=score,
                comment=comment,
                evidence_quote=quote,
                # 来源类型由服务端按报告 MIME 确定，模型不能自行决定
                evidence_source_type=report.source_type,
                evidence_location_start=start,
                evidence_location_end=end,
                error_type=raw.error_type.strip(),
                improvement_suggestion=raw.improvement_suggestion.strip(),
            )
        )

    validated.sort(key=lambda item: item.order)
    return ValidatedGrade(summary=generated.summary.strip(), items=validated)


def build_prompt(*, rubric: list[RubricItemSnapshot], report: ReportSource) -> str:
    """构造用户提示：评分规则 + **按来源位置标注**的报告原文。"""
    lines = [
        f"- rubric_item_id={item.rubric_item_id}｜满分 {item.max_score}"
        f"｜{item.title}：{item.description}"
        for item in rubric
    ]
    blocks = [f"[{report.source_label} {location}] {text}" for location, text in report.units]
    return _USER_PROMPT_TEMPLATE.format(
        rubric="\n".join(lines),
        report="\n".join(blocks),
        source_label=report.source_label,
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
        raise GradeGenerationError("模型输出不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise GradeGenerationError("模型输出不是 JSON 对象")
    return parsed


def grade_submission(
    *,
    rubric: list[RubricItemSnapshot],
    report: ReportSource,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> ValidatedGrade:
    """调用模型批改报告并完成服务端校验（同步，Worker 在线程池中执行）。

    :raises GradeModelNotConfiguredError: 未配置模型端点或模型名称。
    :raises GradeGenerationError: 评分规则或报告为空、请求失败、超时或输出非法。
    """
    if not rubric:
        raise GradeGenerationError("提交没有可用的评分规则")
    if not report.units:
        raise GradeGenerationError("报告没有可用于批改的文本内容")
    if not base_url.strip():
        raise GradeModelNotConfiguredError("报告批改未配置模型端点（AI_BASE_URL）")
    if not model.strip():
        raise GradeModelNotConfiguredError("报告批改未配置模型名称（AI_MODEL）")

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(rubric=rubric, report=report)},
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
            raise GradeGenerationError("模型服务请求超时") from exc
        except httpx.HTTPError as exc:
            raise GradeGenerationError(
                f"模型服务连接失败（{type(exc).__name__}）"
            ) from exc

        if response.status_code in (401, 403):
            raise GradeGenerationError("模型服务拒绝了访问凭据")
        if response.status_code != 200:
            raise GradeGenerationError(
                f"模型服务返回非预期状态 {response.status_code}"
            )

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise GradeGenerationError("模型服务响应格式无效") from exc

        payload_output = _extract_json(content)
        try:
            generated = GeneratedGrade.model_validate(payload_output)
        except ValidationError as exc:
            raise GradeGenerationError("模型输出未通过结构校验") from exc

        validated = validate_generated(generated, rubric=rubric, report=report)
        return replace(validated, raw_output=payload_output)
    finally:
        if owned:
            client.close()


__all__ = [
    "GeneratedGrade",
    "GeneratedGradeItem",
    "GradeGenerationError",
    "GradeModelNotConfiguredError",
    "PROMPT_VERSION",
    "ReportSource",
    "RubricItemSnapshot",
    "ValidatedGrade",
    "ValidatedGradeItem",
    "build_prompt",
    "grade_submission",
    "validate_generated",
]
