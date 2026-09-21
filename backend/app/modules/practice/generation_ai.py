"""练习生成的 AI 适配层（``docs/api-contract.md`` 7.10）。

可替换的适配层：生产环境调用 Chat Completions 兼容端点（复用
``AI_BASE_URL`` / ``AI_MODEL`` / ``AI_API_KEY`` / ``AI_TIMEOUT_SECONDS``），
测试注入 ``httpx.MockTransport`` 支撑的假模型服务。

服务端职责（模型只负责"出题"，配额与来源由服务端兜住）：

1. 题型配额由 :func:`app.modules.practice.scoring.allocate_question_counts`
   决定，模型输出的题型数量必须与配额**完全一致**；
2. 单选题 2–6 个**规范化后不重复**的选项、合法正确项下标；选项 ID 由
   服务端生成（模型只给文本）；
3. 判断题必须给布尔答案；简答题必须给标准答案与 2–6 个评分要点；
4. 每题必须给出解析、来源片段 ID 与原文摘录，且**摘录必须能在对应片段
   原文中找到**、片段必须来自本次上下文，否则整次生成失败；
5. 任何一项不满足即整次失败（``FAILED``），不落库部分题目。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from app.modules.practice.models import PracticeDifficulty, PracticeQuestionType
from app.modules.practice.schemas import GeneratedPractice
from app.modules.practice.scoring import normalize_text

logger = logging.getLogger("app.practice.ai")

#: 提示词版本：写入生成尝试记录
PROMPT_VERSION = "practice-generate-v1"

#: 单选题的选项数量范围
MIN_OPTIONS = 2
MAX_OPTIONS = 6

#: 简答题评分要点数量范围
MIN_GRADING_POINTS = 2
MAX_GRADING_POINTS = 6

SYSTEM_PROMPT = (
    "你是课程出题助手。你只能依据用户提供的课件片段出题，"
    "不得引入片段之外的知识，也不得编造来源。"
)

_USER_PROMPT_TEMPLATE = """请依据下面的课件片段出题。

难度：{difficulty}
需要生成的题目数量与题型分配：
{allocations}

课件片段（只能引用这些片段）：

{chunks}

要求：
1. 只依据上面的片段出题，题目与答案都能在片段中找到依据；
2. 使用与片段相同的语言；
3. 单选题给 2–6 个选项（只给文本，不要给编号），correct_option_index 是正确项下标（从 0 开始）；
4. 判断题用 correct_boolean；简答题用 correct_text 与 2–6 个 grading_points，每个要点含 point（说明）与 accepted（可接受短语数组）；
5. 每题都要给 explanation（解析）、knowledge_point（知识点，可空）、source_chunk_id（上面出现过的片段 ID）与 source_quote（逐字摘自该片段原文的摘录）；
6. 只输出 JSON 对象，不要输出其他文字或代码块围栏；
JSON 格式：
{{"title": "练习标题", "questions": [{{"type": "SINGLE_CHOICE", "prompt": "...", "options": [{{"text": "..."}}], "correct_option_index": 0, "explanation": "...", "knowledge_point": "...", "source_chunk_id": "片段 ID", "source_quote": "原文摘录"}}]}}"""

_TYPE_LABEL = {
    PracticeQuestionType.SINGLE_CHOICE: "SINGLE_CHOICE（单选）",
    PracticeQuestionType.TRUE_FALSE: "TRUE_FALSE（判断）",
    PracticeQuestionType.SHORT_ANSWER: "SHORT_ANSWER（简答）",
}


class PracticeModelNotConfiguredError(Exception):
    """模型端点或模型名称未配置（Worker 按失败处理并给出安全摘要）。"""


class PracticeGenerationError(Exception):
    """模型请求失败或输出非法（消息可安全展示）。"""


@dataclass(frozen=True, slots=True)
class ContextChunk:
    """本次生成可引用的片段（已按资料顺序轮询选取）。"""

    chunk_id: uuid.UUID
    material_id: uuid.UUID
    material_name: str
    content: str
    location_start: int
    location_end: int


@dataclass(frozen=True, slots=True)
class ValidatedQuestion:
    """通过服务端校验的题目（含服务端生成的选项 ID 与来源快照）。"""

    order: int
    type: PracticeQuestionType
    prompt: str
    options: list[dict]
    correct_answer: object
    explanation: str
    knowledge_point: str | None
    grading_points: list[dict]
    source_material_id: uuid.UUID
    source_material_name: str
    source_location_start: int
    source_location_end: int
    source_quote: str


@dataclass(frozen=True, slots=True)
class ValidatedPractice:
    """整份练习（标题 + 全部题目）。"""

    title: str
    questions: list[ValidatedQuestion]


def _normalized_options(options: list) -> list[str]:
    return [normalize_text(option.text) for option in options]


def _validate_question(
    question, *, chunks_by_id: dict[str, ContextChunk], order: int
) -> ValidatedQuestion:
    chunk = chunks_by_id.get(question.source_chunk_id.strip().lower())
    if chunk is None:
        raise PracticeGenerationError("模型引用了本次上下文之外的片段")

    normalized_quote = normalize_text(question.source_quote)
    if not normalized_quote or normalized_quote not in normalize_text(chunk.content):
        raise PracticeGenerationError("模型给出的原文摘录无法在片段中找到")

    base = {
        "order": order,
        "prompt": question.prompt.strip(),
        "explanation": question.explanation.strip(),
        "knowledge_point": (
            question.knowledge_point.strip() if question.knowledge_point else None
        ),
        "source_material_id": chunk.material_id,
        "source_material_name": chunk.material_name,
        "source_location_start": chunk.location_start,
        "source_location_end": chunk.location_end,
        "source_quote": question.source_quote.strip(),
    }

    if question.type is PracticeQuestionType.SINGLE_CHOICE:
        if not (MIN_OPTIONS <= len(question.options) <= MAX_OPTIONS):
            raise PracticeGenerationError("单选题必须包含 2–6 个选项")
        normalized = _normalized_options(question.options)
        if len(set(normalized)) != len(normalized):
            raise PracticeGenerationError("单选题选项规范化后必须互不重复")
        index = question.correct_option_index
        if index is None or not (0 <= index < len(question.options)):
            raise PracticeGenerationError("单选题的正确选项下标非法")
        options = [
            {"id": str(uuid.uuid4()), "text": option.text.strip()}
            for option in question.options
        ]
        return ValidatedQuestion(
            **base,
            type=question.type,
            options=options,
            correct_answer=options[index]["id"],
            grading_points=[],
        )

    if question.type is PracticeQuestionType.TRUE_FALSE:
        if question.correct_boolean is None:
            raise PracticeGenerationError("判断题必须给出布尔答案")
        return ValidatedQuestion(
            **base,
            type=question.type,
            options=[],
            correct_answer=question.correct_boolean,
            grading_points=[],
        )

    if not question.correct_text or not question.correct_text.strip():
        raise PracticeGenerationError("简答题必须给出标准答案")
    if not (MIN_GRADING_POINTS <= len(question.grading_points) <= MAX_GRADING_POINTS):
        raise PracticeGenerationError("简答题必须包含 2–6 个评分要点")
    grading_points = []
    for point in question.grading_points:
        accepted = [phrase.strip() for phrase in point.accepted if phrase.strip()]
        if not accepted:
            raise PracticeGenerationError("评分要点必须给出至少一个可接受短语")
        grading_points.append({"point": point.point.strip(), "accepted": accepted})
    return ValidatedQuestion(
        **base,
        type=question.type,
        options=[],
        correct_answer=question.correct_text.strip(),
        grading_points=grading_points,
    )


def validate_generated(
    generated: GeneratedPractice,
    *,
    chunks: list[ContextChunk],
    allocations: dict[PracticeQuestionType, int],
) -> ValidatedPractice:
    """校验模型输出并生成服务端的题目快照。

    :raises PracticeGenerationError: 任一项不符合要求（整次生成失败）。
    """
    expected_total = sum(allocations.values())
    if len(generated.questions) != expected_total:
        raise PracticeGenerationError("模型给出的题目数量与请求不一致")

    actual: dict[PracticeQuestionType, int] = {}
    for question in generated.questions:
        actual[question.type] = actual.get(question.type, 0) + 1
    if actual != {key: value for key, value in allocations.items() if value}:
        raise PracticeGenerationError("模型给出的题型数量与服务端分配不一致")

    chunks_by_id = {str(chunk.chunk_id).lower(): chunk for chunk in chunks}
    questions = [
        _validate_question(question, chunks_by_id=chunks_by_id, order=index)
        for index, question in enumerate(generated.questions, start=1)
    ]
    return ValidatedPractice(title=generated.title.strip(), questions=questions)


def build_prompt(
    *,
    difficulty: PracticeDifficulty,
    allocations: dict[PracticeQuestionType, int],
    chunks: list[ContextChunk],
) -> str:
    """构造用户提示：只包含本次上下文片段与题型配额。"""
    allocation_lines = [
        f"- {_TYPE_LABEL[question_type]}：{count} 题"
        for question_type, count in allocations.items()
        if count
    ]
    blocks = []
    for index, chunk in enumerate(chunks, start=1):
        blocks.append(
            f"[片段 {index}] chunk_id={chunk.chunk_id}\n"
            f"资料：{chunk.material_name}（位置 {chunk.location_start}-{chunk.location_end}）\n"
            f"原文：\n{chunk.content}"
        )
    return _USER_PROMPT_TEMPLATE.format(
        difficulty=difficulty.value,
        allocations="\n".join(allocation_lines),
        chunks="\n\n".join(blocks),
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
        raise PracticeGenerationError("模型输出不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise PracticeGenerationError("模型输出不是 JSON 对象")
    return parsed


def generate_practice(
    chunks: list[ContextChunk],
    *,
    question_count: int,
    question_types: list[PracticeQuestionType],
    difficulty: PracticeDifficulty,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> ValidatedPractice:
    """调用模型生成练习并完成服务端校验（同步，Worker 在线程池中执行）。

    :raises PracticeModelNotConfiguredError: 未配置模型端点或模型名称。
    :raises PracticeGenerationError: 上下文为空、请求失败、超时或输出非法。
    """
    from app.modules.practice.scoring import allocate_question_counts

    if not chunks:
        raise PracticeGenerationError("没有可用于出题的课件片段")
    if not base_url.strip():
        raise PracticeModelNotConfiguredError("练习生成未配置模型端点（AI_BASE_URL）")
    if not model.strip():
        raise PracticeModelNotConfiguredError("练习生成未配置模型名称（AI_MODEL）")

    allocations = allocate_question_counts(question_count, question_types)
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_prompt(
                    difficulty=difficulty, allocations=allocations, chunks=chunks
                ),
            },
        ],
        "temperature": 0.4,
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
            raise PracticeGenerationError("模型服务请求超时") from exc
        except httpx.HTTPError as exc:
            raise PracticeGenerationError(
                f"模型服务连接失败（{type(exc).__name__}）"
            ) from exc

        if response.status_code in (401, 403):
            raise PracticeGenerationError("模型服务拒绝了访问凭据")
        if response.status_code != 200:
            raise PracticeGenerationError(
                f"模型服务返回非预期状态 {response.status_code}"
            )

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise PracticeGenerationError("模型服务响应格式无效") from exc

        try:
            generated = GeneratedPractice.model_validate(_extract_json(content))
        except ValidationError as exc:
            raise PracticeGenerationError("模型输出未通过结构校验") from exc

        return validate_generated(
            generated, chunks=chunks, allocations=allocations
        )
    finally:
        if owned:
            client.close()
