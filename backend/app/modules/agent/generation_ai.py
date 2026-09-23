"""Agent 的模型适配层（``docs/local-development-agent-backend.md`` 第 6.7 节 +
``docs/agent-backend-implementation-review.md`` 第一节）。

复用现有的 Chat Completions 兼容端点配置（``AI_BASE_URL`` / ``AI_MODEL`` /
``AI_API_KEY``），但使用 **Agent 自己的超时**（``AGENT_MODEL_TIMEOUT_SECONDS``），
因为一次上下文总结可能耗时数分钟，而同步问答的 60 秒显然不够。

受约束的生成（与 chat 的回答校验同源，但**依据策略不同**）：

1. 提示词只包含本次解析出的来源块与业务对象摘要；
2. 模型输出必须是合法 JSON 且通过 :class:`GeneratedAgentAnswer` 校验；
3. 服务端校验引用的 **ref 必须是本次注入的编号**，``quote`` 必须能在该块文本中
   找到（空白规范化后子串匹配）；越界或对不上即**只丢弃这一条引用**；
4. ``groundable`` 的来源（资料、作业说明、评分标准）都能支撑"有依据"的回答；
   只有 ``display_kind`` 非空的来源才成为用户可见引用；
5. **不再"没有引用就整段作废"**（评审文档「一、#1.4 / #1.5」）：
   - 有有效引用 → 保留正文，依据等级由模型自评 + 引用被拒情况决定；
   - 无有效引用但模型确实回答了、且本次注入了可信依据 → 保留正文，
     附加一句"引用未核对上"的说明并置 ``grounded=false``（不谎称有依据）；
   - 两者都不成立 → 用**具体**的无依据说明替换（说清检索了几份资料）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from app.modules.agent.context import ContextBlock, ResolvedContext
from app.modules.agent.models import AgentEvidenceLevel, AgentRunAction
from app.modules.agent.prompts import build_system_prompt, build_user_prompt
from app.modules.agent.schemas import GeneratedAgentAnswer

logger = logging.getLogger("app.agent.ai")

#: 单条摘录入库的长度上限
QUOTE_MAX_LENGTH = 300

#: 连接建立失败的重试次数与退避（秒）。
#: 只重试**连接建立**阶段的失败——此时请求还没有送达模型，重试不会产生
#: 重复计费，也不会有"半次生成"的歧义；读超时（模型可能已开始生成）不重试。
CONNECT_RETRY_ATTEMPTS = 3
CONNECT_RETRY_BACKOFF_SECONDS = (0.5, 1.5)

#: 引用的摘录没核对上时保留正文所用的说明。
#: 这句话必须诚实：说明"内容可能不完整、引用没核对上"，不能暗示已有依据。
UNCORROBORATED_NOTE = "（说明：这次回答里引用的原文没能与课程资料核对上，内容仅供参考，请自行确认。）"


class AgentModelNotConfiguredError(Exception):
    """模型端点或模型名称未配置（Worker 按失败处理，写安全摘要）。"""


class AgentGenerationError(Exception):
    """模型请求失败、超时或输出无效。消息可安全展示。"""


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


def validate_answer(
    answer: GeneratedAgentAnswer,
    context: ResolvedContext,
    *,
    no_evidence_message: str,
) -> ValidatedAgentAnswer:
    """按本次注入的来源校验引用，并执行 grounding policy（文档 6.7 + 评审文档 #1/#2）。

    关键点：**只丢弃核对不上的那一条引用**，不再因为它而抹掉整段回答。
    正文是否保留由 :func:`_decide_content` 按"本次是否注入了可信依据"和
    模型自评的依据等级决定。
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

    # 没有一条引用通过校验。两种截然不同的情况要分开处理：
    # 1) 模型确实给出了回答、本次也注入了可信依据、且它没自评"无依据"
    #    → 保留正文（部分命中时不要把已确认的部分抹掉），但如实标注未核对上；
    # 2) 其余情况 → 用具体的无依据说明替换。
    if (
        text
        and declared != AgentEvidenceLevel.NONE.value
        and context.has_groundable_evidence()
    ):
        return f"{text}\n\n{UNCORROBORATED_NOTE}", False, AgentEvidenceLevel.NONE.value

    return no_evidence_message, False, AgentEvidenceLevel.NONE.value


def _post_with_connect_retry(
    client: httpx.Client, url: str, payload: dict, headers: dict
) -> httpx.Response | None:
    """POST 请求；只对**连接建立失败**做有界重试。

    - 连接建立失败（``ConnectError`` / ``ConnectTimeout``）说明请求没送达模型，
      重试安全且大概率能成功；
    - 读超时说明模型可能已经开始生成，重试会重复消耗额度，因此直接上抛；
    - 其他 HTTP 错误原样上抛。
    """
    last_error: Exception | None = None
    for attempt in range(CONNECT_RETRY_ATTEMPTS):
        try:
            return client.post(url, json=payload, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_error = exc
            logger.warning(
                "模型服务连接失败（第 %s/%s 次尝试）：%s",
                attempt + 1,
                CONNECT_RETRY_ATTEMPTS,
                type(exc).__name__,
            )
            if attempt < len(CONNECT_RETRY_BACKOFF_SECONDS):
                time.sleep(CONNECT_RETRY_BACKOFF_SECONDS[attempt])
        except httpx.TimeoutException as exc:
            raise AgentGenerationError("模型服务请求超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise AgentGenerationError(
                f"模型服务连接失败（{type(exc).__name__}），请稍后重试"
            ) from exc

    raise AgentGenerationError(
        f"模型服务连接失败（{type(last_error).__name__ if last_error else 'ConnectError'}），"
        f"已重试 {CONNECT_RETRY_ATTEMPTS} 次，请稍后重试"
    )


def generate_answer(
    context: ResolvedContext,
    *,
    action: AgentRunAction,
    question: str,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    no_evidence_message: str,
    output_language: str | None = None,
    client: httpx.Client | None = None,
) -> ValidatedAgentAnswer:
    """调用模型生成回答并完成服务端校验（同步，Worker 在线程池中执行）。

    :param no_evidence_message: 确实无依据时的替换文案。由调用方按"检索了哪些
        资料"拼出来，因此比一句固定文案更有信息量（评审文档「一、#1」）。
    :param output_language: 用户在 ``options.output_language`` 指定的输出语言；
        为空时沿用"与用户输入同语言"（评审文档「一、#12」）。
    :raises AgentModelNotConfiguredError: 未配置模型端点或模型名称。
    :raises AgentGenerationError: 请求失败、超时或输出无效。
    """
    if not context.blocks:
        # 没有任何可注入内容：不调用模型，也不要求模型配置（与 chat 一致）
        return ValidatedAgentAnswer(
            content=no_evidence_message,
            grounded=False,
            citations=[],
            evidence_level=AgentEvidenceLevel.NONE.value,
        )
    if not base_url.strip():
        raise AgentModelNotConfiguredError("Agent 未配置模型端点（AI_BASE_URL）")
    if not model.strip():
        raise AgentModelNotConfiguredError("Agent 未配置模型名称（AI_MODEL）")

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": build_system_prompt(action)},
            {
                "role": "user",
                "content": build_user_prompt(
                    context, question=question, output_language=output_language
                ),
            },
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
        response = _post_with_connect_retry(client, url, payload, headers)

        if response.status_code in (401, 403):
            raise AgentGenerationError("模型服务拒绝了访问凭据")
        if response.status_code != 200:
            raise AgentGenerationError(f"模型服务返回非预期状态 {response.status_code}")

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AgentGenerationError("模型服务响应格式无效") from exc

        try:
            answer = GeneratedAgentAnswer.model_validate(_extract_json(content))
        except ValidationError as exc:
            raise AgentGenerationError("模型输出未通过结构校验") from exc

        return validate_answer(answer, context, no_evidence_message=no_evidence_message)
    finally:
        if owned:
            client.close()
