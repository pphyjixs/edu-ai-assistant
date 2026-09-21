"""练习请求校验与模型输出约束的离线单元测试（不访问数据库与网络）。

覆盖契约 7.2（生成请求边界）、7.6（提交答案结构）与 7.10（模型输出校验）：

- 生成请求：资料数量与重复、题型非空与去重、题数边界与"不少于题型数量"、
  显式 ``null`` 拒绝；
- 模型输出：数量与题型配额、单选选项数量/重复/下标、判断题布尔、
  简答题评分要点、来源片段与摘录核对、选项 ID 由服务端生成。
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError

from app.modules.practice import generation_ai
from app.modules.practice.models import PracticeDifficulty, PracticeQuestionType
from app.modules.practice.schemas import (
    GeneratedPractice,
    PracticeAttemptAnswerRequest,
    PracticeAttemptSubmitRequest,
    PracticeGenerateRequest,
)

SINGLE = PracticeQuestionType.SINGLE_CHOICE
TRUE_FALSE = PracticeQuestionType.TRUE_FALSE
SHORT = PracticeQuestionType.SHORT_ANSWER

CONTENT = "软件工程是应用系统化的方法。需求分析是软件生命周期的起点。"


def _chunk(
    *,
    chunk_id: uuid.UUID | None = None,
    material_id: uuid.UUID | None = None,
    content: str = CONTENT,
    name: str = "chapter-1.docx",
    location: tuple[int, int] = (1, 4),
) -> generation_ai.ContextChunk:
    return generation_ai.ContextChunk(
        chunk_id=chunk_id or uuid.uuid4(),
        material_id=material_id or uuid.uuid4(),
        material_name=name,
        content=content,
        location_start=location[0],
        location_end=location[1],
    )


def _single_question(chunk_id: uuid.UUID, *, options: list[str] | None = None, index: int = 0) -> dict:
    return {
        "type": "SINGLE_CHOICE",
        "prompt": "软件工程的定义是什么？",
        "options": [{"text": text} for text in (options or ["系统化的方法", "随意的编码"])],
        "correct_option_index": index,
        "explanation": "课件第一句给出定义。",
        "knowledge_point": "软件工程",
        "source_chunk_id": str(chunk_id),
        "source_quote": "软件工程是应用系统化的方法。",
    }


# --------------------------------------------------------------------------- #
# 生成请求校验（契约 7.2）
# --------------------------------------------------------------------------- #
def _generate_payload(**overrides) -> dict:
    payload = {
        "material_ids": [str(uuid.uuid4())],
        "question_count": 5,
        "question_types": ["SINGLE_CHOICE", "TRUE_FALSE"],
        "difficulty": "MEDIUM",
    }
    payload.update(overrides)
    return payload


def test_generate_request_accepts_valid_payload() -> None:
    request = PracticeGenerateRequest.model_validate(_generate_payload())
    assert request.question_count == 5
    assert len(request.material_ids) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"material_ids": []},
        {"material_ids": [str(uuid.uuid4())] * 11},
        {"material_ids": [str(uuid.uuid4())] * 2},  # 重复
        {"question_count": 0},
        {"question_count": 21},
        {"question_count": 1, "question_types": ["SINGLE_CHOICE", "TRUE_FALSE"]},
        {"question_types": []},
        {"question_types": ["SINGLE_CHOICE", "SINGLE_CHOICE"]},
        {"question_types": ["ESSAY"]},
        {"difficulty": "INSANE"},
        {"difficulty": None},
        {"material_ids": None},
    ],
)
def test_generate_request_rejects_invalid_payload(overrides: dict) -> None:
    with pytest.raises(PydanticValidationError):
        PracticeGenerateRequest.model_validate(_generate_payload(**overrides))


def test_generate_request_rejects_unknown_fields() -> None:
    with pytest.raises(PydanticValidationError):
        PracticeGenerateRequest.model_validate(
            _generate_payload(temperature=0.5)
        )


def test_submit_request_rejects_duplicate_questions() -> None:
    question_id = str(uuid.uuid4())
    with pytest.raises(PydanticValidationError):
        PracticeAttemptSubmitRequest.model_validate(
            {
                "answers": [
                    {"question_id": question_id, "answer": True},
                    {"question_id": question_id, "answer": False},
                ]
            }
        )


def test_submit_request_rejects_empty_and_bad_answer_types() -> None:
    with pytest.raises(PydanticValidationError):
        PracticeAttemptSubmitRequest.model_validate({"answers": []})
    with pytest.raises(PydanticValidationError):
        PracticeAttemptAnswerRequest.model_validate(
            {"question_id": str(uuid.uuid4()), "answer": 42}
        )


# --------------------------------------------------------------------------- #
# 模型输出校验（契约 7.10）
# --------------------------------------------------------------------------- #
def test_validate_generated_accepts_well_formed_output() -> None:
    chunk = _chunk()
    generated = GeneratedPractice.model_validate(
        {
            "title": "软件工程练习",
            "questions": [
                _single_question(chunk.chunk_id),
                {
                    "type": "TRUE_FALSE",
                    "prompt": "需求分析是软件生命周期的起点。",
                    "correct_boolean": True,
                    "explanation": "课件第二句。",
                    "knowledge_point": "需求分析",
                    "source_chunk_id": str(chunk.chunk_id),
                    "source_quote": "需求分析是软件生命周期的起点。",
                },
            ],
        }
    )

    validated = generation_ai.validate_generated(
        generated,
        chunks=[chunk],
        allocations={SINGLE: 1, TRUE_FALSE: 1},
    )

    assert validated.title == "软件工程练习"
    assert [item.order for item in validated.questions] == [1, 2]
    # 选项 ID 由服务端生成，并作为单选的标准答案
    first = validated.questions[0]
    assert len(first.options) == 2
    assert first.correct_answer == first.options[0]["id"]
    assert first.source_material_id == chunk.material_id
    assert first.source_material_name == "chapter-1.docx"
    assert first.source_location_start == 1
    # 判断题没有选项与评分要点
    assert validated.questions[1].options == []
    assert validated.questions[1].correct_answer is True


def test_validate_generated_rejects_wrong_question_count() -> None:
    chunk = _chunk()
    generated = GeneratedPractice.model_validate(
        {"title": "t", "questions": [_single_question(chunk.chunk_id)]}
    )
    with pytest.raises(generation_ai.PracticeGenerationError):
        generation_ai.validate_generated(
            generated, chunks=[chunk], allocations={SINGLE: 2}
        )


def test_validate_generated_rejects_wrong_type_allocation() -> None:
    chunk = _chunk()
    generated = GeneratedPractice.model_validate(
        {"title": "t", "questions": [_single_question(chunk.chunk_id)]}
    )
    with pytest.raises(generation_ai.PracticeGenerationError):
        generation_ai.validate_generated(
            generated, chunks=[chunk], allocations={TRUE_FALSE: 1}
        )


@pytest.mark.parametrize("index", [-1, 2, 99])
def test_validate_generated_rejects_bad_option_index(index: int) -> None:
    chunk = _chunk()
    generated = GeneratedPractice.model_validate(
        {
            "title": "t",
            "questions": [_single_question(chunk.chunk_id, index=index)],
        }
    )
    with pytest.raises(generation_ai.PracticeGenerationError):
        generation_ai.validate_generated(
            generated, chunks=[chunk], allocations={SINGLE: 1}
        )


def test_validate_generated_rejects_duplicate_options() -> None:
    chunk = _chunk()
    generated = GeneratedPractice.model_validate(
        {
            "title": "t",
            "questions": [
                _single_question(chunk.chunk_id, options=["同一个", "同一个"])
            ],
        }
    )
    with pytest.raises(generation_ai.PracticeGenerationError):
        generation_ai.validate_generated(
            generated, chunks=[chunk], allocations={SINGLE: 1}
        )


def test_validate_generated_rejects_hallucinated_quote() -> None:
    chunk = _chunk()
    question = _single_question(chunk.chunk_id)
    question["source_quote"] = "课件里并不存在的句子"
    generated = GeneratedPractice.model_validate(
        {"title": "t", "questions": [question]}
    )
    with pytest.raises(generation_ai.PracticeGenerationError):
        generation_ai.validate_generated(
            generated, chunks=[chunk], allocations={SINGLE: 1}
        )


def test_validate_generated_rejects_unknown_chunk() -> None:
    chunk = _chunk()
    generated = GeneratedPractice.model_validate(
        {
            "title": "t",
            "questions": [_single_question(uuid.uuid4())],
        }
    )
    with pytest.raises(generation_ai.PracticeGenerationError):
        generation_ai.validate_generated(
            generated, chunks=[chunk], allocations={SINGLE: 1}
        )


def test_validate_generated_requires_short_answer_points() -> None:
    chunk = _chunk()
    generated = GeneratedPractice.model_validate(
        {
            "title": "t",
            "questions": [
                {
                    "type": "SHORT_ANSWER",
                    "prompt": "简述软件工程的定义。",
                    "correct_text": "应用系统化的方法。",
                    "grading_points": [
                        {"point": "系统化", "accepted": ["系统化"]},
                    ],
                    "explanation": "课件第一句。",
                    "source_chunk_id": str(chunk.chunk_id),
                    "source_quote": "软件工程是应用系统化的方法。",
                }
            ],
        }
    )
    with pytest.raises(generation_ai.PracticeGenerationError):
        generation_ai.validate_generated(
            generated, chunks=[chunk], allocations={SHORT: 1}
        )


# --------------------------------------------------------------------------- #
# 适配层：配置缺失与失败分支
# --------------------------------------------------------------------------- #
def test_generate_practice_requires_model_configuration() -> None:
    chunk = _chunk()
    with pytest.raises(generation_ai.PracticeModelNotConfiguredError):
        generation_ai.generate_practice(
            [chunk],
            question_count=1,
            question_types=[SINGLE],
            difficulty=PracticeDifficulty.EASY,
            base_url="",
            api_key="",
            model="m",
            timeout_seconds=1.0,
        )


def test_generate_practice_requires_context() -> None:
    with pytest.raises(generation_ai.PracticeGenerationError):
        generation_ai.generate_practice(
            [],
            question_count=1,
            question_types=[SINGLE],
            difficulty=PracticeDifficulty.EASY,
            base_url="http://fake-model.local/v1",
            api_key="",
            model="m",
            timeout_seconds=1.0,
        )


def _model_response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]
        },
    )


def test_generate_practice_through_fake_model() -> None:
    """经假模型端点走通完整校验（配额正确、摘录可核对）。"""
    chunk = _chunk()
    captured: list[dict] = []

    def responder(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return _model_response(
            {"title": "练习", "questions": [_single_question(chunk.chunk_id)]}
        )

    validated = generation_ai.generate_practice(
        [chunk],
        question_count=1,
        question_types=[SINGLE],
        difficulty=PracticeDifficulty.MEDIUM,
        base_url="http://fake-model.local/v1",
        api_key="k",
        model="m",
        timeout_seconds=1.0,
        client=httpx.Client(transport=httpx.MockTransport(responder)),
    )

    assert len(validated.questions) == 1
    assert validated.questions[0].prompt
    # 提示词包含配额说明与片段 ID，供模型引用
    prompt = captured[0]["messages"][1]["content"]
    assert str(chunk.chunk_id) in prompt
    assert "SINGLE_CHOICE" in prompt


def test_generate_practice_rejects_model_failures() -> None:
    chunk = _chunk()

    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(generation_ai.PracticeGenerationError):
        generation_ai.generate_practice(
            [chunk],
            question_count=1,
            question_types=[SINGLE],
            difficulty=PracticeDifficulty.EASY,
            base_url="http://fake-model.local/v1",
            api_key="",
            model="m",
            timeout_seconds=1.0,
            client=httpx.Client(transport=httpx.MockTransport(server_error)),
        )

    def invalid_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "不是 JSON"}}]}
        )

    with pytest.raises(generation_ai.PracticeGenerationError):
        generation_ai.generate_practice(
            [chunk],
            question_count=1,
            question_types=[SINGLE],
            difficulty=PracticeDifficulty.EASY,
            base_url="http://fake-model.local/v1",
            api_key="",
            model="m",
            timeout_seconds=1.0,
            client=httpx.Client(transport=httpx.MockTransport(invalid_json)),
        )
