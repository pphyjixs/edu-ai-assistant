"""问答检索与引用校验的离线单元测试（不访问数据库与网络）。

覆盖契约 6.1 的受约束生成要求：

- 关键词构造（ASCII 词 + 中文 2-gram）与数量上限；
- 提示词只包含本次检索到的片段；
- 引用校验：片段 ID 必须来自本次检索、摘录必须能在片段原文中找到；
- 全部引用无效时按无依据处理（固定文案 + 空引用）。
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.modules.chat import answer_ai, retrieval
from app.modules.chat.schemas import (
    NO_EVIDENCE_ANSWER,
    GeneratedAnswer,
    GeneratedCitation,
)


def _chunk(
    content: str,
    *,
    chunk_id: uuid.UUID | None = None,
    material_id: uuid.UUID | None = None,
    location_start: int = 1,
    location_end: int = 1,
) -> retrieval.RetrievedChunk:
    return retrieval.RetrievedChunk(
        chunk_id=chunk_id or uuid.uuid4(),
        material_id=material_id or uuid.uuid4(),
        material_name="chapter-1.docx",
        content_type=(
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        ),
        content=content,
        location_start=location_start,
        location_end=location_end,
        score=0.5,
    )


# --------------------------------------------------------------------------- #
# 关键词构造
# --------------------------------------------------------------------------- #
def test_search_terms_keep_ascii_words_and_cjk_bigrams() -> None:
    terms = retrieval.build_search_terms("What is software engineering 软件工程")

    assert "what" in terms
    assert "software" in terms
    assert "软件" in terms
    assert "件工" in terms
    # 单字符不参与检索（噪声太大）
    assert "的" not in terms


def test_search_terms_are_deduplicated_and_capped() -> None:
    question = "".join(f"w{i} " for i in range(40)) + "软件工程软件工程"

    terms = retrieval.build_search_terms(question)

    assert len(terms) <= retrieval.MAX_SEARCH_TERMS
    assert len(set(terms)) == len(terms)


def test_search_terms_reject_single_char_queries() -> None:
    assert retrieval.build_search_terms("的") == []


# --------------------------------------------------------------------------- #
# 提示词只包含检索片段
# --------------------------------------------------------------------------- #
def test_prompt_contains_only_retrieved_chunks() -> None:
    chunk = _chunk("软件工程是应用系统化的方法。", location_start=2, location_end=3)

    prompt = answer_ai.build_prompt("什么是软件工程？", [chunk])

    assert str(chunk.chunk_id) in prompt
    assert chunk.content in prompt
    assert "什么是软件工程？" in prompt
    # 不含片段之外的资料内容（提示词只能来自检索结果）
    assert "其他课程的机密内容" not in prompt


# --------------------------------------------------------------------------- #
# 引用校验
# --------------------------------------------------------------------------- #
def test_valid_citation_is_kept_and_normalized() -> None:
    chunk = _chunk("软件工程是应用系统化的方法。\n需求分析是起点。")
    answer = GeneratedAnswer(
        answer="按课件，软件工程是应用系统化的方法。",
        citations=[
            GeneratedCitation(
                chunk_id=str(chunk.chunk_id),
                # 跨行摘录：规范化空白后仍能命中
                quote="软件工程是应用系统化的方法。 需求分析是起点。",
            )
        ],
    )

    validated = answer_ai.validate_answer(answer, [chunk])

    assert validated.grounded is True
    assert validated.content.startswith("按课件")
    assert len(validated.citations) == 1
    assert validated.citations[0].chunk.chunk_id == chunk.chunk_id


def test_citation_outside_retrieved_chunks_is_dropped() -> None:
    chunk = _chunk("软件工程是应用系统化的方法。")
    answer = GeneratedAnswer(
        answer="编造的回答",
        citations=[
            GeneratedCitation(chunk_id=str(uuid.uuid4()), quote="任意摘录")
        ],
    )

    validated = answer_ai.validate_answer(answer, [chunk])

    # 只引用本次检索之外的片段 → 视为无依据，不输出编造来源
    assert validated.grounded is False
    assert validated.content == NO_EVIDENCE_ANSWER
    assert validated.citations == []


def test_hallucinated_quote_is_dropped() -> None:
    chunk = _chunk("软件工程是应用系统化的方法。")
    answer = GeneratedAnswer(
        answer="编造的回答",
        citations=[
            GeneratedCitation(
                chunk_id=str(chunk.chunk_id), quote="这句话在原文里并不存在"
            )
        ],
    )

    validated = answer_ai.validate_answer(answer, [chunk])

    assert validated.grounded is False
    assert validated.citations == []


def test_partial_valid_citations_keep_only_valid_ones() -> None:
    good = _chunk("需求分析是软件生命周期的起点。")
    bad = _chunk("另一段原文。")
    answer = GeneratedAnswer(
        answer="回答",
        citations=[
            GeneratedCitation(chunk_id=str(bad.chunk_id), quote="不在原文里的摘录"),
            GeneratedCitation(chunk_id=str(good.chunk_id), quote="需求分析是软件生命周期的起点。"),
        ],
    )

    validated = answer_ai.validate_answer(answer, [good, bad])

    assert validated.grounded is True
    assert [item.chunk.chunk_id for item in validated.citations] == [good.chunk_id]


def test_no_chunks_means_no_evidence_without_calling_model() -> None:
    """没有检索到片段时不调用模型，直接返回无依据（契约 6.1）。"""

    def responder(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("没有片段时不应调用模型")

    validated = answer_ai.generate_answer(
        [],
        question="随便问",
        base_url="http://fake-model.local/v1",
        api_key="",
        model="fake-model",
        timeout_seconds=1.0,
        client=httpx.Client(transport=httpx.MockTransport(responder)),
    )

    assert validated.grounded is False
    assert validated.content == NO_EVIDENCE_ANSWER


# --------------------------------------------------------------------------- #
# 模型适配层：配置缺失与输出校验
# --------------------------------------------------------------------------- #
def test_missing_model_configuration_raises_not_configured() -> None:
    chunk = _chunk("原文")
    with pytest.raises(answer_ai.AnswerModelNotConfiguredError):
        answer_ai.generate_answer(
            [chunk],
            question="问题",
            base_url="",
            api_key="",
            model="m",
            timeout_seconds=1.0,
        )
    with pytest.raises(answer_ai.AnswerModelNotConfiguredError):
        answer_ai.generate_answer(
            [chunk],
            question="问题",
            base_url="http://fake-model.local/v1",
            api_key="",
            model="",
            timeout_seconds=1.0,
        )


def test_invalid_model_output_raises_generation_error() -> None:
    chunk = _chunk("原文")

    def bad_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "不是 JSON"}}]}
        )

    with pytest.raises(answer_ai.AnswerGenerationError):
        answer_ai.generate_answer(
            [chunk],
            question="问题",
            base_url="http://fake-model.local/v1",
            api_key="",
            model="m",
            timeout_seconds=1.0,
            client=httpx.Client(transport=httpx.MockTransport(bad_json)),
        )


def test_model_http_error_raises_generation_error() -> None:
    chunk = _chunk("原文")

    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(answer_ai.AnswerGenerationError):
        answer_ai.generate_answer(
            [chunk],
            question="问题",
            base_url="http://fake-model.local/v1",
            api_key="",
            model="m",
            timeout_seconds=1.0,
            client=httpx.Client(transport=httpx.MockTransport(server_error)),
        )


def test_model_timeout_raises_generation_error() -> None:
    chunk = _chunk("原文")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(answer_ai.AnswerGenerationError):
        answer_ai.generate_answer(
            [chunk],
            question="问题",
            base_url="http://fake-model.local/v1",
            api_key="",
            model="m",
            timeout_seconds=1.0,
            client=httpx.Client(transport=httpx.MockTransport(timeout)),
        )


def test_prompt_version_is_stable() -> None:
    """提示词版本写入生成尝试记录，改名即破坏历史可追溯性。"""
    assert answer_ai.PROMPT_VERSION == "chat-answer-v1"


def test_request_payload_carries_chunk_ids_for_model() -> None:
    """发给模型的请求必须携带片段 ID，模型只能从中选择引用。"""
    chunk = _chunk("软件工程是应用系统化的方法。")
    captured: list[dict] = []

    def capture(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer": "回答",
                                    "citations": [
                                        {
                                            "chunk_id": str(chunk.chunk_id),
                                            "quote": "软件工程是应用系统化的方法。",
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            },
        )

    validated = answer_ai.generate_answer(
        [chunk],
        question="什么是软件工程？",
        base_url="http://fake-model.local/v1",
        api_key="secret-key",
        model="fake-model",
        timeout_seconds=1.0,
        client=httpx.Client(transport=httpx.MockTransport(capture)),
    )

    assert validated.grounded is True
    payload = captured[0]
    assert payload["model"] == "fake-model"
    assert str(chunk.chunk_id) in payload["messages"][1]["content"]
