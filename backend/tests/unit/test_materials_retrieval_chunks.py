"""检索片段切片的离线单元测试（不访问数据库、不解析真实文件）。

契约 6.1 要求问答只检索落库的原文片段；本文件固定切分规则：
约 1,000 字符一片、相邻片段重叠约 100 字符、来源定位随片段一起给出。
"""

from __future__ import annotations

import pytest

from app.modules.materials import extraction

#: 片段目标长度与重叠（与 extraction 默认值一致）
CHUNK = extraction.DEFAULT_RETRIEVAL_CHUNK_CHARS
OVERLAP = extraction.DEFAULT_RETRIEVAL_OVERLAP_CHARS


def _units(count: int, unit_chars: int = 300) -> list[tuple[int, str]]:
    """构造 ``count`` 个来源单元；文本带单元编号，保证子串不会在其他单元重复出现。"""
    return [
        (index, "".join(f"{index}:{i};" for i in range(max(unit_chars // 6, 1))))
        for index in range(1, count + 1)
    ]


def test_chunks_are_about_target_length() -> None:
    """每个片段都不超过目标长度，且尽量接近它（最后一片可以短）。"""
    chunks = extraction.chunk_units_for_retrieval(_units(20), chunk_chars=CHUNK)

    assert chunks, "必须产出片段"
    for chunk in chunks[:-1]:
        assert len(chunk.content) <= CHUNK
        assert len(chunk.content) > CHUNK // 2


def test_adjacent_chunks_overlap_about_100_chars() -> None:
    """相邻片段共享约 100 字符的重叠：跨边界的语义不会丢失。"""
    units = _units(20)
    chunks = extraction.chunk_units_for_retrieval(units)
    assert len(chunks) >= 3, "用例需要至少三片才能比较相邻重叠"

    joined = "\n".join(text for _, text in units)
    for previous, current in zip(chunks, chunks[1:]):
        previous_start = joined.index(previous.content)
        current_start = joined.index(current.content)
        # 片段按原文顺序推进（不重排、不回退）
        assert current_start > previous_start
        # 下一片从上一片结尾前 OVERLAP 个原文字符处开始：重叠约 100 字符
        # （片段内用换行拼接单元，实际重叠会有几个字符的拼接偏差）
        overlap_len = (previous_start + len(previous.content)) - current_start
        assert OVERLAP - 10 <= overlap_len <= OVERLAP + 10, overlap_len


def test_chunks_keep_source_locations() -> None:
    """片段记录来源位置区间：PDF 页码 / PPTX 幻灯片号 / DOCX 段落序号。"""
    chunks = extraction.chunk_units_for_retrieval(_units(12, unit_chars=400))

    for chunk in chunks:
        assert 1 <= chunk.location_start <= chunk.location_end <= 12
    # 第一个片段从第 1 个来源单元开始，最后一个覆盖到末尾
    assert chunks[0].location_start == 1
    assert chunks[-1].location_end == 12


def test_single_long_unit_produces_multiple_chunks() -> None:
    """单个超长来源单元也要切成多片，不能整段塞进一片。"""
    # 文本各不相同，避免重复字符掩盖"是否真的切成多片"
    long_text = "".join(f"{i:04d};" for i in range(CHUNK))
    chunks = extraction.chunk_units_for_retrieval([(1, long_text)])

    assert len(chunks) > 1
    assert all(chunk.location_start == chunk.location_end == 1 for chunk in chunks)
    assert len(set(chunk.content for chunk in chunks)) == len(chunks)


def test_chunk_orders_are_continuous() -> None:
    """片段序号从 1 开始连续（落库即 ``order``，唯一约束依赖它）。"""
    chunks = extraction.chunk_units_for_retrieval(_units(15))
    assert [chunk.index for chunk in chunks] == list(range(1, len(chunks) + 1))


def test_empty_units_produce_no_chunks() -> None:
    assert extraction.chunk_units_for_retrieval([]) == []


@pytest.mark.parametrize(
    ("chunk_chars", "overlap_chars"), [(0, 10), (-1, 0), (100, 100), (100, 200)]
)
def test_invalid_chunk_parameters_are_rejected(
    chunk_chars: int, overlap_chars: int
) -> None:
    with pytest.raises(ValueError):
        extraction.chunk_units_for_retrieval(
            _units(3), chunk_chars=chunk_chars, overlap_chars=overlap_chars
        )


def test_extraction_result_carries_both_chunk_kinds() -> None:
    """Worker 一次提取同时得到模型块与检索片段。"""
    units = _units(10, unit_chars=500)
    result = extraction.ExtractionResult(
        chunks=extraction.chunk_units(
            units,
            source_type=extraction.MaterialSectionSourceType.DOCX_PARAGRAPH,
            chunk_chars=8_000,
        ),
        retrieval_chunks=extraction.chunk_units_for_retrieval(units),
    )

    assert result.chunks, "必须产出模型块"
    assert result.retrieval_chunks, "必须产出检索片段"
    # 两者独立：检索片段更细，数量不会少于模型块
    assert len(result.retrieval_chunks) >= len(result.chunks)
