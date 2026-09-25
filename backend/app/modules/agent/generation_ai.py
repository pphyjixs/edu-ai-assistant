"""Agent 的最终答案解析与引用校验（开发方案 5.1 / 5.5）。

本轮把「只调用一次模型」的传输层下沉到 :mod:`app.modules.agent.model_protocol`，
本模块只保留**服务端校验**这一半，因此它同时服务两条路径：

- 有界循环（:mod:`app.modules.agent.orchestration`）拿到普通 ``assistant.content``
  后调用 :func:`parse_generated_answer` + :func:`validate_answer`；
- 工具产生的证据已经并进同一个 Evidence Ledger，因此**引用校验口径完全不变**。

受约束的生成规则：

1. 模型输出必须是合法 JSON 且通过 :class:`GeneratedAgentAnswer` 校验；
2. 服务端校验引用的 **ref 必须是本次注入的编号**，``quote`` 必须能在该块文本中
   找到（空白规范化后子串匹配）；越界或对不上即**只丢弃这一条引用**；
3. ``groundable`` 的来源（资料、作业说明、评分标准）都能支撑"有依据"的回答；
   只有 ``display_kind`` 非空的来源才成为用户可见引用；
4. **不"没有引用就整段作废"**（评审文档「一、#1.4 / #1.5」）：
   - 有有效引用 → 保留正文，依据等级由模型自评 + 引用被拒情况决定；
   - 无有效引用但模型确实回答了、且本次注入了可信依据 → 保留正文，
     附加一句"引用未核对上"的说明并置 ``grounded=false``（不谎称有依据）；
   - 两者都不成立 → 用**具体**的无依据说明替换（说清检索了几份资料）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from pydantic import ValidationError

from app.modules.agent.context import ContextBlock, ResolvedContext
from app.modules.agent.model_protocol import (
    AgentGenerationError,
    AgentModelNotConfiguredError,
    ModelToolCallUnsupportedError,
)
from app.modules.agent.models import AgentEvidenceLevel
from app.modules.agent.schemas import GeneratedAgentAnswer

logger = logging.getLogger("app.agent.ai")

#: 单条摘录入库的长度上限
QUOTE_MAX_LENGTH = 300

#: 引用的摘录没核对上时保留正文所用的说明。
#: 这句话必须诚实：说明"内容可能不完整、引用没核对上"，不能暗示已有依据。
UNCORROBORATED_NOTE = "（说明：这次回答里引用的原文没能与课程资料核对上，内容仅供参考，请自行确认。）"


@dataclass(frozen=True, slots=True)
class ValidatedAgentCitation:
    """通过校验的引用：来源块 + 可核对摘录。"""

    block: ContextBlock
    quote: str
    #: 用户可见引用的类别（MATERIAL / ASSIGNMENT）；None 表示只用于支撑结论、不展示
    display_kind: str | None = None


@dataclass(frozen=True, slots=True)
class ValidatedAgentAnswer:
    """服务端校验后的回答。"""

    content: str
    grounded: bool
    citations: list[ValidatedAgentCitation]
    #: 依据充分度（``AgentEvidenceLevel`` 的取值）
    evidence_level: str = AgentEvidenceLevel.NONE.value

    @property
    def display_citations(self) -> list[ValidatedAgentCitation]:
        """能落库成用户可见引用的部分（作业引用也能展示，只是不跳转）。"""
        return [item for item in self.citations if item.display_kind]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _extract_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentGenerationError("模型输出不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise AgentGenerationError("模型输出不是 JSON 对象")
    return parsed


def parse_generated_answer(content: str) -> GeneratedAgentAnswer:
    """解析模型的最终回答。

    :raises AgentGenerationError: 不是合法 JSON 或未通过结构校验。
    """
    try:
        return GeneratedAgentAnswer.model_validate(_extract_json(content))
    except ValidationError as exc:
        raise AgentGenerationError("模型输出未通过结构校验") from exc


def validate_answer(
    answer: GeneratedAgentAnswer,
    context: ResolvedContext,
    *,
    no_evidence_message: str,
    keep_answer_without_evidence: bool = False,
) -> ValidatedAgentAnswer:
    """按本次注入的来源校验引用，并执行 grounding policy（文档 6.7 + 评审文档 #1/#2）。

    关键点：**只丢弃核对不上的那一条引用**，不再因为它而抹掉整段回答。

    :param keep_answer_without_evidence: 本次 Run 是否**执行过工具调用**。
        工具结果（成功的检索、权限拒绝、错误码）本身就是模型回答的合法依据来源，
        因此这时即使一条引用都没通过校验，也必须保留模型的正文——
        否则「这个功能仅限课程教师」「我没有这个工具」这类回答会被替换成
        "没有找到依据"，用户看到的是一句与问题无关的话（开发方案第 8 节：
        工具权限不足应作为正常助手回答解释）。
    """
    by_ref = context.block_by_ref()
    validated: list[ValidatedAgentCitation] = []
    rejected = 0
    seen: set[str] = set()

    for citation in answer.citations:
        ref = citation.ref.strip().upper()
        block = by_ref.get(ref)
        if block is None:
            logger.warning("模型引用了本次上下文之外的编号 %s，已丢弃", ref)
            rejected += 1
            continue
        if not block.groundable:
            # 课程摘要、用户选中文本不是可信依据（评审文档「一、#2」）
            logger.info("来源 %s 不能支撑结论，已丢弃该引用", ref)
            rejected += 1
            continue
        normalized_quote = _normalize(citation.quote)
        if not normalized_quote or normalized_quote not in _normalize(block.text):
            logger.warning("模型摘录无法在来源 %s 的原文中找到，已丢弃", ref)
            rejected += 1
            continue
        if ref in seen:
            continue
        seen.add(ref)
        quote = citation.quote.strip()
        validated.append(
            ValidatedAgentCitation(
                block=block,
                quote=quote if len(quote) <= QUOTE_MAX_LENGTH else quote[:QUOTE_MAX_LENGTH],
                display_kind=block.display_kind,
            )
        )

    declared = answer.evidence_level
    content, grounded, level = _decide_content(
        answer=answer,
        context=context,
        validated=validated,
        rejected=rejected,
        declared=declared,
        no_evidence_message=no_evidence_message,
        keep_answer_without_evidence=keep_answer_without_evidence,
    )
    if rejected:
        # 评审文档「一、#1.7」：记录被拒原因，但不记录原文与密钥
        logger.info(
            "本次共丢弃 %s 条无法核对的引用（注入 %s 个块，通过 %s 条）",
            rejected,
            len(context.blocks),
            len(validated),
        )
    return ValidatedAgentAnswer(
        content=content,
        grounded=grounded,
        citations=validated if grounded else [],
        evidence_level=level,
    )


def _decide_content(
    *,
    answer: GeneratedAgentAnswer,
    context: ResolvedContext,
    validated: list[ValidatedAgentCitation],
    rejected: int,
    declared: str | None,
    no_evidence_message: str,
    keep_answer_without_evidence: bool = False,
) -> tuple[str, bool, str]:
    """grounding policy：决定保留正文、标记依据等级，还是替换为无依据说明。"""
    text = answer.answer.strip()

    if validated:
        if declared == AgentEvidenceLevel.PARTIAL.value:
            level = AgentEvidenceLevel.PARTIAL.value
        elif rejected:
            # 模型声称完整，但有一部分引用核对不上：降级为部分依据
            level = AgentEvidenceLevel.PARTIAL.value
        else:
            level = AgentEvidenceLevel.FULL.value
        return text, True, level

    # 没有一条引用通过校验。三种情况要分开处理：
    # 0) 本次执行过工具：正文是在解释工具结果（成功检索、权限拒绝、错误码），
    #    必须保留——替换成"没有找到依据"会答非所问；
    # 1) 模型确实给出了回答、本次也注入了可信依据、且它没自评"无依据"
    #    → 保留正文（部分命中时不要把已确认的部分抹掉），但如实标注未核对上；
    # 2) 其余情况 → 用具体的无依据说明替换。
    if text and keep_answer_without_evidence:
        return text, False, AgentEvidenceLevel.NONE.value

    if (
        text
        and declared != AgentEvidenceLevel.NONE.value
        and context.has_groundable_evidence()
    ):
        return f"{text}\n\n{UNCORROBORATED_NOTE}", False, AgentEvidenceLevel.NONE.value

    return no_evidence_message, False, AgentEvidenceLevel.NONE.value


__all__ = [
    "QUOTE_MAX_LENGTH",
    "UNCORROBORATED_NOTE",
    "AgentGenerationError",
    "AgentModelNotConfiguredError",
    "ModelToolCallUnsupportedError",
    "ValidatedAgentAnswer",
    "ValidatedAgentCitation",
    "parse_generated_answer",
    "validate_answer",
]
