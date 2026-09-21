"""出题上下文的选择与预算（契约 7.10，纯函数、不接触数据库）。

要求：

- 任一所选资料没有可用片段时**安全失败**（返回空），不调用模型；
- ``max_chars`` 为正且至少能为每份资料保留内容；
- 每份所选资料都必须在预算内得到至少一个上下文片段，不得静默遗漏；
- 最终文本长度严格不超过 ``max_chars``；
- 来源摘录的核对基于**截断后**的文本（返回的 chunk 内容即截断结果）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.modules.practice import worker


@dataclass(frozen=True, slots=True)
class FakeChunk:
    """与 Worker 读取的片段行同形的测试替身。"""

    material_id: uuid.UUID
    filename: str
    chunk_id: uuid.UUID
    order: int
    location_start: int
    location_end: int
    content: str


def _rows(
    material_id: uuid.UUID, name: str, contents: list[str]
) -> list[FakeChunk]:
    return [
        FakeChunk(
            material_id=material_id,
            filename=name,
            chunk_id=uuid.uuid4(),
            order=index,
            location_start=index,
            location_end=index,
            content=content,
        )
        for index, content in enumerate(contents, start=1)
    ]


def _materials(count: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(count)]


def test_every_material_is_covered_within_budget() -> None:
    """三份资料、预算充足：每份都有内容，总量不超上限。"""
    ids = _materials(3)
    rows = (
        _rows(ids[0], "a.docx", ["甲" * 100])
        + _rows(ids[1], "b.docx", ["乙" * 100])
        + _rows(ids[2], "c.docx", ["丙" * 100])
    )

    selected = worker.select_context(rows, material_order=ids, max_chars=60)

    assert {chunk.material_id for chunk in selected} == set(ids), "每份资料都必须有上下文"
    assert sum(len(chunk.content) for chunk in selected) <= 60


def test_budget_smaller_than_first_chunk_still_respects_limit() -> None:
    """预算小于首片段长度：截断后仍覆盖每份资料，且绝不超限。"""
    ids = _materials(2)
    rows = _rows(ids[0], "a.docx", ["甲" * 500]) + _rows(
        ids[1], "b.docx", ["乙" * 500]
    )

    selected = worker.select_context(rows, material_order=ids, max_chars=10)

    assert {chunk.material_id for chunk in selected} == set(ids)
    assert sum(len(chunk.content) for chunk in selected) == 10
    assert len(selected) == 2


def test_context_never_exceeds_budget_for_many_chunks() -> None:
    """片段很多时也严格不超预算，且按资料轮询其次序稳定。"""
    ids = _materials(3)
    rows = []
    for material_id in ids:
        rows.extend(_rows(material_id, "m.docx", ["字" * 40 for _ in range(5)]))

    selected = worker.select_context(rows, material_order=ids, max_chars=100)

    assert sum(len(chunk.content) for chunk in selected) <= 100
    assert {chunk.material_id for chunk in selected} == set(ids)
    # 第一轮每份资料各取一个片段（顺序与资料顺序一致）
    assert [chunk.material_id for chunk in selected[:3]] == ids


def test_second_round_appends_in_round_robin_order() -> None:
    """第二轮按资料轮询追加后续片段，最后一个片段按剩余预算截断。"""
    ids = _materials(2)
    rows = _rows(ids[0], "a.docx", ["甲" * 10, "乙" * 10]) + _rows(
        ids[1], "b.docx", ["丙" * 10, "丁" * 10]
    )

    selected = worker.select_context(rows, material_order=ids, max_chars=35)

    assert sum(len(chunk.content) for chunk in selected) == 35
    # 第一轮各 10 字符（共 20），第二轮先给第一份 10 字符，再给第二份 5 字符（截断）
    assert [len(chunk.content) for chunk in selected] == [10, 10, 10, 5]
    assert [chunk.material_id for chunk in selected] == [ids[0], ids[1], ids[0], ids[1]]


def test_missing_chunks_for_any_material_returns_empty() -> None:
    """任一所选资料没有可用片段 → 空结果（调用方安全失败，不调用模型）。"""
    ids = _materials(2)
    rows = _rows(ids[0], "a.docx", ["甲" * 20])  # 第二份资料没有任何片段

    assert worker.select_context(rows, material_order=ids, max_chars=100) == []


def test_blank_chunk_content_does_not_count_as_coverage() -> None:
    """只有空白内容的片段不算覆盖该资料。"""
    ids = _materials(1)
    rows = _rows(ids[0], "a.docx", ["   ", "\n"])

    assert worker.select_context(rows, material_order=ids, max_chars=50) == []


def test_non_positive_or_too_small_budget_returns_empty() -> None:
    """``max_chars`` 非正、或不足以给每份资料留内容时返回空。"""
    ids = _materials(3)
    rows = []
    for material_id in ids:
        rows.extend(_rows(material_id, "m.docx", ["字" * 50]))

    assert worker.select_context(rows, material_order=ids, max_chars=0) == []
    assert worker.select_context(rows, material_order=ids, max_chars=-5) == []
    assert worker.select_context(rows, material_order=ids, max_chars=2) == []
    # 恰好等于资料数：每份资料只能拿到 1 个字符
    selected = worker.select_context(rows, material_order=ids, max_chars=3)
    assert len(selected) == 3
    assert all(len(chunk.content) == 1 for chunk in selected)


def test_exactly_one_char_per_material_is_supported() -> None:
    """边界：预算恰好等于资料数时仍覆盖全部资料。"""
    ids = _materials(2)
    rows = _rows(ids[0], "a.docx", ["甲乙丙"]) + _rows(ids[1], "b.docx", ["丁戊己"])

    selected = worker.select_context(rows, material_order=ids, max_chars=2)

    assert {chunk.material_id for chunk in selected} == set(ids)
    assert sum(len(chunk.content) for chunk in selected) == 2


def test_selected_content_is_the_truncated_text() -> None:
    """返回的 content 就是传给模型的文本（摘录核对以它为准）。"""
    ids = _materials(1)
    rows = _rows(ids[0], "a.docx", ["软件工程是应用系统化的方法。"])

    selected = worker.select_context(rows, material_order=ids, max_chars=6)

    assert selected[0].content == "软件工程是应"  # 恰好 6 个字符
    assert selected[0].location_start == 1
    assert selected[0].material_name == "a.docx"
