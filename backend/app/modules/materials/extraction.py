"""课件文本提取：从文件字节流抽取带来源位置的文本块（契约 5.5 第 2 步）。

职责边界与旧版（确定性大纲生成）不同：本模块只负责**提取与分块**，
章节标题与知识点由模型生成（:mod:`app.modules.materials.outline_ai`）。

- **PDF**（``pypdf``）：逐页抽取文本，来源位置为页码（从 1 开始）；
  扫描版（图片型）页面抽取不到文本——整份文件无文本时按解析失败处理，
  **不提供图片 OCR**（pypdf 不支持，契约明确排除）。
- **PPTX**（``python-pptx``）：每张幻灯片一个文本单元，位置为幻灯片号。
- **DOCX**（``python-docx``）：逐段落抽取文本，位置为段落序号（从 1 开始）。

全文按来源顺序拼接后切分为**文本块**：每块不超过
``chunk_chars``（默认 8,000）字符，块内保留各来源位置区间；全文超过
``max_chars``（默认 120,000）字符时抛 :class:`ExtractLimitExceeded`——
任务直接进入 FAILED，**不截断后宣称成功**（契约 5.5）。
"""

from __future__ import annotations

import hashlib
import io
import re

from app.modules.materials.schemas import MaterialSectionSourceType

#: 全文上限与单块上限的默认值（与 Settings 同步；测试可覆盖）
DEFAULT_MAX_CHARS = 120_000
DEFAULT_CHUNK_CHARS = 8_000

#: 单个来源单元（页/张/段）的最大字符数，超出部分截断并记警告
_UNIT_CHAR_LIMIT = 8_000


class ExtractError(Exception):
    """提取失败：文件损坏或无文本。消息可安全展示（契约 5.5 第 4 步）。"""


class ExtractLimitExceeded(ExtractError):
    """全文超过 ``max_chars`` 上限：直接 FAILED，不截断。"""


def sha256_of(data: bytes) -> str:
    """计算十六进制 SHA-256（小写）。"""
    return hashlib.sha256(data).hexdigest()


def source_type_for(content_type: str) -> MaterialSectionSourceType:
    """由规范 MIME 推导来源类型。"""
    mapping = {
        "application/pdf": MaterialSectionSourceType.PDF_PAGE,
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
            MaterialSectionSourceType.PPTX_SLIDE
        ),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
            MaterialSectionSourceType.DOCX_PARAGRAPH
        ),
    }
    source_type = mapping.get(content_type)
    if source_type is None:
        raise ExtractError("不支持的文件类型，无法解析")
    return source_type


# --------------------------------------------------------------------------- #
# 提取：格式 → [(位置, 文本), ...]
# --------------------------------------------------------------------------- #
def _extract_pdf(data: bytes) -> list[tuple[int, str]]:
    """逐页抽取文本；无任何文本（扫描版）时明确失败，不做 OCR。"""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - pypdf 的损坏文件异常族很宽
        raise ExtractError("PDF 文件损坏，无法解析") from exc

    pages: list[tuple[int, str]] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001 - 单页损坏不拖垮整份文件
            text = ""
        if text:
            pages.append((index, text[:_UNIT_CHAR_LIMIT]))

    if not pages:
        raise ExtractError(
            "未能从 PDF 中抽取到文本内容；扫描版（图片型）PDF 暂不支持解析"
        )
    return pages


def _extract_pptx(data: bytes) -> list[tuple[int, str]]:
    """每张幻灯片一个文本单元；无文本时失败。"""
    from pptx import Presentation

    try:
        presentation = Presentation(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - python-pptx 的损坏文件异常族很宽
        raise ExtractError("演示文稿文件损坏，无法解析") from exc

    slides: list[tuple[int, str]] = []
    for index, slide in enumerate(presentation.slides, start=1):
        parts: list[str] = []
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for paragraph in shape.text_frame.paragraphs:
                line = " ".join(
                    run.text for run in paragraph.runs
                ).strip()
                if line:
                    parts.append(line)
        text = "\n".join(parts).strip()
        if text:
            slides.append((index, text[:_UNIT_CHAR_LIMIT]))

    if not slides:
        raise ExtractError("未能从演示文稿中抽取到文本内容")
    return slides


def _extract_docx(data: bytes) -> list[tuple[int, str]]:
    """逐段落抽取文本，位置为段落序号（含空段落计数，保证定位稳定）。"""
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - python-docx 的损坏文件异常族很宽
        raise ExtractError("文档文件损坏，无法解析") from exc

    paragraphs: list[tuple[int, str]] = []
    for index, paragraph in enumerate(document.paragraphs, start=1):
        text = paragraph.text.strip()
        if text:
            paragraphs.append((index, text[:_UNIT_CHAR_LIMIT]))

    if not paragraphs:
        raise ExtractError("未能从文档中抽取到文本内容")
    return paragraphs


def extract_units(data: bytes, *, content_type: str) -> list[tuple[int, str]]:
    """提取 ``(来源位置, 文本)`` 序列；位置从 1 开始、按来源顺序排列。"""
    source_type = source_type_for(content_type)
    if source_type is MaterialSectionSourceType.PDF_PAGE:
        return _extract_pdf(data)
    if source_type is MaterialSectionSourceType.PPTX_SLIDE:
        return _extract_pptx(data)
    return _extract_docx(data)


# --------------------------------------------------------------------------- #
# 分块
# --------------------------------------------------------------------------- #
class TextChunk:
    """一个送入模型的文本块。

    :ivar index: 从 1 开始的块序号
    :ivar location_start / location_end: 块覆盖的来源位置区间（从 1 开始）
    :ivar source_type: 来源类型（块的单位由格式决定）
    :ivar text: 块文本（含位置标记行，供模型与摘录核对）
    :ivar body: 纯文本内容（不含位置标记行，用于摘录子串校验）
    """

    __slots__ = ("index", "location_start", "location_end", "source_type", "text", "body")

    def __init__(
        self,
        *,
        index: int,
        location_start: int,
        location_end: int,
        source_type: MaterialSectionSourceType,
        text: str,
        body: str,
    ) -> None:
        self.index = index
        self.location_start = location_start
        self.location_end = location_end
        self.source_type = source_type
        self.text = text
        self.body = body


def _location_label(source_type: MaterialSectionSourceType) -> str:
    return {
        MaterialSectionSourceType.PDF_PAGE: "页",
        MaterialSectionSourceType.PPTX_SLIDE: "幻灯片",
        MaterialSectionSourceType.DOCX_PARAGRAPH: "段落",
    }[source_type]


def chunk_units(
    units: list[tuple[int, str]],
    *,
    source_type: MaterialSectionSourceType,
    max_chars: int = DEFAULT_MAX_CHARS,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
) -> list[TextChunk]:
    """按来源顺序把文本单元切分为块（契约 5.5：每块 8,000、全文 120,000）。

    超过 ``max_chars`` 时抛 :class:`ExtractLimitExceeded`——**不截断**，
    任务进入 FAILED 并给出明确原因。
    """
    total = sum(len(text) for _, text in units)
    if total > max_chars:
        raise ExtractLimitExceeded(
            f"文件文本量（{total:,} 字符）超出解析上限（{max_chars:,} 字符），"
            "请拆分后再上传"
        )

    label = _location_label(source_type)
    chunks: list[TextChunk] = []
    current_lines: list[str] = []
    current_body: list[str] = []
    current_start = units[0][0] if units else 1
    current_end = current_start
    current_len = 0

    def flush() -> None:
        nonlocal current_lines, current_body, current_len
        if not current_lines:
            return
        index = len(chunks) + 1
        body = "\n".join(current_body)
        header = (
            f"[来源：{_location_label(source_type)} "
            f"{current_start}–{current_end}；块 {index}]"
        )
        chunks.append(
            TextChunk(
                index=index,
                location_start=current_start,
                location_end=current_end,
                source_type=source_type,
                text=f"{header}\n{body}",
                body=body,
            )
        )
        current_lines = []
        current_body = []
        current_len = 0

    for location, text in units:
        if current_len + len(text) > chunk_chars and current_body:
            flush()
            current_start = location
        line = f"[{label} {location}] {text}"
        current_lines.append(line)
        current_body.append(text)
        current_len += len(line)
        current_end = location
    flush()

    if not chunks:  # pragma: no cover - extract_units 已保证非空
        raise ExtractError("未能抽取到文本内容")
    return chunks


def extract_and_chunk(
    data: bytes,
    *,
    content_type: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
) -> list[TextChunk]:
    """提取文本并按来源顺序分块（Worker 主入口）。"""
    units = extract_units(data, content_type=content_type)
    return chunk_units(
        units,
        source_type=source_type_for(content_type),
        max_chars=max_chars,
        chunk_chars=chunk_chars,
    )


# --------------------------------------------------------------------------- #
# 摘录核对
# --------------------------------------------------------------------------- #
def _normalize_for_match(text: str) -> str:
    """摘录核对用规范化：压缩全部空白（含换行），保证跨行摘录可命中。"""
    return re.sub(r"\s+", "", text)


class QuoteNotInSourceError(ValueError):
    """模型输出的摘录无法在对应来源文本中找到（无效输出）。"""


def verify_quote_in_source(quote: str, source_text: str) -> None:
    """校验摘录确实出现在来源文本中（空白规范化后子串匹配）。

    :raises QuoteNotInSourceError: 摘录不是来源的子串（模型幻觉）。
    """
    normalized_quote = _normalize_for_match(quote)
    if not normalized_quote:
        raise QuoteNotInSourceError("原文摘录为空")
    if normalized_quote not in _normalize_for_match(source_text):
        raise QuoteNotInSourceError("原文摘录无法在来源文本中找到")
