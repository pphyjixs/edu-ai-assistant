"""课件解析器：从文件字节流抽取章节与知识点（契约 5.5 第 2 步）。

第一版采用**确定性**抽取策略，不依赖 AI 服务、不做 OCR（契约 5.5 明确排除
扫描版 PDF 的 OCR）：每种格式按其原生结构拆出章节，标题取结构化标题或
条目首行，知识点取章节内的内容条目，原文摘录直接取自对应位置。

- **PDF**（``pypdf``）：逐页抽取文本；页面首行（优先编号标题行）作为章节标题。
- **PPTX**（``zipfile`` + ``ElementTree``）：每张幻灯片一个章节，
  标题取幻灯片第一个文本块，其余文本条目作为知识点。
- **DOCX**（``zipfile`` + ``ElementTree``）：Heading 样式段落拆章节，
  章节内普通段落作为知识点。

PPTX 与 DOCX 本质是 ZIP 内的 XML，用标准库解析即可，不引入 lxml 依赖。
解析结果以纯函数输出 :class:`ExtractedSection`，与数据库、网络完全解耦，
单元测试可以针对构造的字节流直接断言。
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree
from zipfile import BadZipFile

from app.modules.materials.schemas import MaterialSectionSourceType

#: 章节/知识点文本的最大长度（与模型列宽一致，超长截断）
_TEXT_MAX_LENGTH = 2000

#: 单个文件最多抽取的章节数，防止异常大文件拖垮解析
_MAX_SECTIONS = 200

#: 单个章节最多抽取的知识点数
_MAX_POINTS_PER_SECTION = 20

#: PDF 文本抽取时每页最多读取的字符数
_PDF_PAGE_CHAR_LIMIT = 8000


@dataclass(slots=True)
class ExtractedKnowledgePoint:
    """解析出的知识点原文结构。"""

    title: str
    description: str
    quote: str
    location_start: int
    location_end: int


@dataclass(slots=True)
class ExtractedSection:
    """解析出的章节原文结构。"""

    title: str
    source_type: MaterialSectionSourceType
    location_start: int
    location_end: int
    knowledge_points: list[ExtractedKnowledgePoint] = field(default_factory=list)


class ParseFailedError(Exception):
    """解析失败：无文本内容、格式损坏或解析器异常。

    ``message`` 会被 Worker 原样写入 ``error_message`` / ``job.error``，
    必须是可安全展示的中文摘要，不含堆栈与内部路径（契约 5.5 第 4 步）。
    """


def _clean(text: str, *, limit: int = _TEXT_MAX_LENGTH) -> str:
    """合并空白并截断到安全长度。"""
    cleaned = " ".join(text.split())
    return cleaned[:limit]


#: 章节编号：阿拉伯数字或中文数字（「1.2」「第一章」「三、」）
_CHAPTER_NUMBER = r"(?:\d+|[一二三四五六七八九十百]+)"


def _split_heading(title: str) -> str:
    """把「1.2 标题正文」「第一章 绪论」拆出标题部分；没有编号时原样返回。"""
    pattern = (
        rf"^(?:第?\s*{_CHAPTER_NUMBER}(?:[.、]\s*{_CHAPTER_NUMBER})*"
        rf"\s*[章节讲．.]?\s*)(.*)$"
    )
    match = re.match(pattern, title)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return title


# --------------------------------------------------------------------------- #
# PPTX：每张幻灯片一个章节
# --------------------------------------------------------------------------- #
_PPT_TEXT_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"


def _parse_pptx(data: bytes) -> list[ExtractedSection]:
    """每张幻灯片一个章节：标题 = 第一个文本块，知识点 = 其余文本块。"""
    sections: list[ExtractedSection] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            slide_names = sorted(
                name
                for name in archive.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            )
            if not slide_names:
                raise ParseFailedError("演示文稿中没有可解析的幻灯片")

            for index, name in enumerate(slide_names, start=1):
                root = ElementTree.fromstring(archive.read(name))
                texts = [
                    _clean(node.text or "")
                    for node in root.iter(_PPT_TEXT_NS)
                    if node.text and node.text.strip()
                ]
                texts = [text for text in texts if text]
                if not texts:
                    continue

                title = texts[0]
                points = [
                    ExtractedKnowledgePoint(
                        title=text[:120],
                        description=text,
                        quote=text,
                        location_start=index,
                        location_end=index,
                    )
                    for text in texts[1 : 1 + _MAX_POINTS_PER_SECTION]
                ]
                sections.append(
                    ExtractedSection(
                        title=title[:255],
                        source_type=MaterialSectionSourceType.PPTX_SLIDE,
                        location_start=index,
                        location_end=index,
                        knowledge_points=points,
                    )
                )
                if len(sections) >= _MAX_SECTIONS:
                    break
    except zipfile.BadZipFile as exc:
        raise ParseFailedError("演示文稿文件损坏，无法解析") from exc
    except ElementTree.ParseError as exc:
        raise ParseFailedError("演示文稿内容异常，无法解析") from exc

    if not sections:
        raise ParseFailedError("未能从演示文稿中抽取到文本内容")
    return sections


# --------------------------------------------------------------------------- #
# DOCX：Heading 段落拆章节，普通段落作为知识点
# --------------------------------------------------------------------------- #
_DOCX_TEXT_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCX_PARAGRAPH_TAG = f"{_DOCX_TEXT_NS}p"
_DOCX_STYLE_TAG = f"{_DOCX_TEXT_NS}pStyle"
_DOCX_TEXT_TAG = f"{_DOCX_TEXT_NS}t"


def _docx_paragraph_style(paragraph: ElementTree.Element) -> str:
    style = paragraph.find(f"./{_DOCX_TEXT_NS}pPr/{_DOCX_STYLE_TAG}")
    if style is None:
        return ""
    return style.get(f"{_DOCX_TEXT_NS}val", "")


def _parse_docx(data: bytes) -> list[ExtractedSection]:
    """Heading 1–3 段落拆章节，章节内普通段落作为知识点。

    段落定位以文档中的段落序号（从 1 开始）计，与契约 5.4 的
    ``DOCX_PARAGRAPH`` 定位规则一致。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except KeyError as exc:
        raise ParseFailedError("文档缺少正文内容，无法解析") from exc
    except (BadZipFile, ElementTree.ParseError) as exc:
        raise ParseFailedError("文档文件损坏，无法解析") from exc

    sections: list[ExtractedSection] = []
    current: ExtractedSection | None = None
    paragraph_number = 0

    for paragraph in root.iter(_DOCX_PARAGRAPH_TAG):
        paragraph_number += 1
        text = _clean(
            "".join(node.text or "" for node in paragraph.iter(_DOCX_TEXT_TAG))
        )
        if not text:
            continue

        style = _docx_paragraph_style(paragraph).lower()
        is_heading = re.fullmatch(r"heading[123]", style)
        if is_heading or current is None:
            current = ExtractedSection(
                title=_split_heading(text)[:255],
                source_type=MaterialSectionSourceType.DOCX_PARAGRAPH,
                location_start=paragraph_number,
                location_end=paragraph_number,
            )
            sections.append(current)
            if len(sections) >= _MAX_SECTIONS:
                break
            continue

        current.location_end = paragraph_number
        if len(current.knowledge_points) < _MAX_POINTS_PER_SECTION:
            current.knowledge_points.append(
                ExtractedKnowledgePoint(
                    title=text[:120],
                    description=text,
                    quote=text,
                    location_start=paragraph_number,
                    location_end=paragraph_number,
                )
            )

    if not sections:
        raise ParseFailedError("未能从文档中抽取到文本内容")
    return sections


# --------------------------------------------------------------------------- #
# PDF：逐页抽取文本
# --------------------------------------------------------------------------- #
_PDF_HEADING_PATTERN = re.compile(
    r"^\s*(?:第?\s*\d+(?:[.、]\d+)*\s*[章节讲．.]?)\s*\S+"
)


def _pdf_page_title(page_text: str) -> str | None:
    """取页面中符合「编号标题」特征的首行；没有则返回 None。"""
    for line in page_text.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        if _PDF_HEADING_PATTERN.match(cleaned):
            return cleaned
        return None
    return None


def _parse_pdf(data: bytes) -> list[ExtractedSection]:
    """逐页一个章节：页面首行（优先编号标题行）作为章节标题。

    扫描版（图片型）页面抽取不到文本，按空页跳过；整份文件没有任何
    文本时按解析失败处理（契约 5.5：第一版不做 OCR）。
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 依赖缺失属于部署错误
        raise ParseFailedError("解析服务未就绪，请稍后重试") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
        if not reader.pages:
            raise ParseFailedError("PDF 中没有可解析的页面")
    except ParseFailedError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf 的损坏文件异常族很宽
        raise ParseFailedError("PDF 文件损坏，无法解析") from exc

    sections: list[ExtractedSection] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - 单页损坏不拖垮整份文件
            text = ""
        text = text[:_PDF_PAGE_CHAR_LIMIT].strip()
        if not text:
            continue

        body_lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = _pdf_page_title(text) or body_lines[0]
        points = [
            ExtractedKnowledgePoint(
                title=line[:120],
                description=line,
                quote=line,
                location_start=index,
                location_end=index,
            )
            for line in body_lines[1 : 1 + _MAX_POINTS_PER_SECTION]
            if line != title
        ]
        sections.append(
            ExtractedSection(
                title=title[:255],
                source_type=MaterialSectionSourceType.PDF_PAGE,
                location_start=index,
                location_end=index,
                knowledge_points=points,
            )
        )
        if len(sections) >= _MAX_SECTIONS:
            break

    if not sections:
        raise ParseFailedError(
            "未能从 PDF 中抽取到文本内容；扫描版（图片型）PDF 暂不支持解析"
        )
    return sections


def extract_outline(*, content_type: str, data: bytes) -> list[ExtractedSection]:
    """按规范 MIME 分发解析；返回按资料顺序排列的章节列表。

    :raises ParseFailedError: 文件损坏或抽取不到文本（消息可安全展示）。
    """
    if content_type == "application/pdf":
        return _parse_pdf(data)
    if content_type == (
        "application/vnd.openxmlformats-officedocument"
        ".presentationml.presentation"
    ):
        return _parse_pptx(data)
    if content_type == (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document"
    ):
        return _parse_docx(data)
    raise ParseFailedError("不支持的文件类型，无法解析")
