"""课件解析器单元测试（契约 5.5，不接触数据库与网络）。

PPTX/DOCX 是 ZIP 内的 XML，直接在测试里构造最小合法字节流；
PDF 侧重点断言分发与失败路径（扫描版无文本 → 解析失败），
不追求复刻 pypdf 的内部行为。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.modules.materials.parser import (
    ExtractedSection,
    ParseFailedError,
    extract_outline,
)
from app.modules.materials.schemas import (
    MaterialSectionSourceType,
    source_type_for_content_type,
)

PDF_MIME = "application/pdf"
PPTX_MIME = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


# --------------------------------------------------------------------------- #
# 字节流构造
# --------------------------------------------------------------------------- #
def build_docx(paragraphs: list[tuple[str, str | None]]) -> bytes:
    """构造最小 DOCX：``(文本, Heading 样式或 None)`` 列表。"""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = ""
    for text, style in paragraphs:
        style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body += f"<w:p>{style_xml}<w:r><w:t>{text}</w:t></w:r></w:p>"
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def build_pptx(slides: list[list[str]]) -> bytes:
    """构造最小 PPTX：每张幻灯片是一组文本块。"""
    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index, texts in enumerate(slides, start=1):
            paragraphs = "".join(
                f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" for text in texts
            )
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<p:sld xmlns:a="{ns}" xmlns:p="urn:x" name="slide {index}">'
                f"<p:cSld>{paragraphs}</p:cSld></p:sld>",
            )
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 来源类型推导
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        (PDF_MIME, MaterialSectionSourceType.PDF_PAGE),
        (PPTX_MIME, MaterialSectionSourceType.PPTX_SLIDE),
        (DOCX_MIME, MaterialSectionSourceType.DOCX_PARAGRAPH),
    ],
)
def test_source_type_follows_content_type(
    content_type: str, expected: MaterialSectionSourceType
) -> None:
    assert source_type_for_content_type(content_type) is expected


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #
def test_docx_heading_splits_sections() -> None:
    data = build_docx(
        [
            ("第一章 绪论", "Heading1"),
            ("软件工程是应用系统化的方法。", None),
            ("过程、方法与工具缺一不可。", None),
            ("1.2 需求分析", "Heading2"),
            ("需求分析是软件生命周期的起点。", None),
        ]
    )

    sections = extract_outline(content_type=DOCX_MIME, data=data)

    assert [section.title for section in sections] == ["绪论", "需求分析"]
    assert all(
        section.source_type is MaterialSectionSourceType.DOCX_PARAGRAPH
        for section in sections
    )
    # 定位从 1 开始：段落序号覆盖章节范围（首章 1–3 段，次章 4–5 段）
    assert (sections[0].location_start, sections[0].location_end) == (1, 3)
    assert (sections[1].location_start, sections[1].location_end) == (4, 5)
    assert len(sections[0].knowledge_points) == 2
    point = sections[0].knowledge_points[0]
    assert point.quote == "软件工程是应用系统化的方法。"
    assert point.location_start == point.location_end == 2


def test_docx_without_headings_still_produces_one_section() -> None:
    data = build_docx([("第一段内容", None), ("第二段内容", None)])

    sections = extract_outline(content_type=DOCX_MIME, data=data)

    assert len(sections) == 1
    assert sections[0].location_start == 1
    assert sections[0].location_end == 2
    assert len(sections[0].knowledge_points) == 1


def test_docx_corrupted_fails_with_safe_message() -> None:
    with pytest.raises(ParseFailedError) as excinfo:
        extract_outline(content_type=DOCX_MIME, data=b"not a zip file")
    assert "损坏" in str(excinfo.value)


def test_docx_without_text_fails() -> None:
    with pytest.raises(ParseFailedError):
        extract_outline(content_type=DOCX_MIME, data=build_docx([]))


# --------------------------------------------------------------------------- #
# PPTX
# --------------------------------------------------------------------------- #
def test_pptx_one_section_per_slide() -> None:
    data = build_pptx(
        [
            ["软件工程概述", "定义与范围", "历史沿革"],
            ["需求工程", "需求获取", "需求验证"],
        ]
    )

    sections = extract_outline(content_type=PPTX_MIME, data=data)

    assert [section.title for section in sections] == ["软件工程概述", "需求工程"]
    assert all(
        section.source_type is MaterialSectionSourceType.PPTX_SLIDE
        for section in sections
    )
    # 每张幻灯片的定位就是幻灯片号（从 1 开始）
    assert [section.location_start for section in sections] == [1, 2]
    assert [len(section.knowledge_points) for section in sections] == [2, 2]
    quote = sections[0].knowledge_points[0].quote
    assert quote == "定义与范围"


def test_pptx_corrupted_fails() -> None:
    with pytest.raises(ParseFailedError):
        extract_outline(content_type=PPTX_MIME, data=b"PK\x03\x04 broken")


def test_pptx_without_slides_fails() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ppt/presentation.xml", "<empty/>")
    with pytest.raises(ParseFailedError):
        extract_outline(content_type=PPTX_MIME, data=buffer.getvalue())


# --------------------------------------------------------------------------- #
# PDF 与分发
# --------------------------------------------------------------------------- #
def test_pdf_without_text_fails_with_ocr_hint() -> None:
    """扫描版（图片型）PDF 抽取不到文本：按解析失败处理，不做 OCR（契约 5.5）。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    data = buffer.getvalue()

    with pytest.raises(ParseFailedError) as excinfo:
        extract_outline(content_type=PDF_MIME, data=data)
    assert "OCR" in str(excinfo.value) or "扫描" in str(excinfo.value)


def test_unknown_content_type_fails() -> None:
    with pytest.raises(ParseFailedError):
        extract_outline(content_type="text/plain", data=b"hello")


def test_sections_are_ordered_and_bounded() -> None:
    data = build_docx(
        [(f"第{i}章 标题{i}", "Heading1") for i in range(1, 12)]
    )
    sections = extract_outline(content_type=DOCX_MIME, data=data)
    assert isinstance(sections, list)
    assert all(isinstance(section, ExtractedSection) for section in sections)
    assert [section.location_start for section in sections] == list(
        range(1, len(sections) + 1)
    )
