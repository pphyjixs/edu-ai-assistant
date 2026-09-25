"""有界模型/工具编排循环（开发方案 5.6 + 5.5 的适配规则）。

职责：

1. 组装首轮消息（固定规则 + 工具定义 + Skill 目录 + 业务上下文 + 输出规则）；
2. 轮流调用模型与执行工具，把工具结果按 ``tool_call_id`` 回传；
3. 把工具产生的证据并入**同一个 Evidence Ledger**，最终答案的引用仍按
   「ref 必须是本次出现过的编号 + quote 必须是该块原文的子串」校验；
4. 落 ``agent_run_steps`` 审计（含终态），并保证写工具幂等。

必须满足的边界（开发方案 5.6）：

================================  =====
每 Run 模型轮次                    6
每 Run 工具总调用                  6
每 Run 写工具成功次数              1
单个工具给模型的 JSON              12 KiB
全部工具结果累计                   32 KiB
单次检索证据                       最多 8 条（由工具输入上限保证）
================================  =====

硬约束：

- 等待模型期间**不持有任何数据库事务**：工具各自开短事务并立即关闭；
- 未注册工具直接返回 ``UNKNOWN_TOOL``，**不做模糊匹配、不动态 import**；
- 工具异常一律收敛成稳定错误码，绝不把堆栈、数据库细节或密钥放进 tool message。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.core.time import utc_now
from app.modules.agent import generation_ai, repository as repo
from app.modules.agent.context import ContextBlock, ResolvedContext
from app.modules.agent.model_protocol import AgentGenerationError, ToolCallDraft, complete
from app.modules.agent.models import (
    STEP_CALL_ID_MAX_LENGTH,
    STEP_ERROR_CODE_MAX_LENGTH,
    STEP_NAME_MAX_LENGTH,
)
from app.modules.agent.prompts import build_system_prompt, build_user_prompt
from app.modules.agent.skills import LOADED_SKILLS_TOTAL_MAX_BYTES
from app.modules.agent.tool_registry import ToolRegistry
from app.modules.agent.tool_types import (
    STEP_PAYLOAD_MAX_BYTES,
    TOOL_RESULT_MAX_BYTES,
    TOOL_RESULTS_TOTAL_MAX_BYTES,
    AgentArtifact,
    StepKind,
    StepStatus,
    ToolContext,
    ToolErrorCode,
    ToolEvidence,
    ToolResult,
    ToolSideEffect,
    ToolSpec,
    has_write_intent,
)

logger = logging.getLogger("app.agent.orchestration")

#: 编排器版本：成功与失败都写进 ``agent_runs.orchestrator_version``
ORCHESTRATOR_VERSION = "agent-tools-v1"

#: 每 Run 最大模型轮次
MAX_MODEL_ROUNDS = 6
#: 每 Run 最大工具调用次数
MAX_TOOL_CALLS = 6
#: 每 Run 最多成功执行的写工具次数
MAX_WRITE_CALLS = 1
#: 参数非法允许模型修正的次数（连续两次无效即结束 Run）
MAX_INVALID_ARGUMENT_RETRIES = 1


class AgentLoopLimitError(AgentGenerationError):
    """有界循环超限（轮次或工具次数）。"""

    code = "AGENT_LOOP_LIMIT"


class AgentToolArgumentsError(AgentGenerationError):
    """模型连续给出非法工具参数。"""

    code = "INVALID_TOOL_ARGUMENTS"


class AgentRunAbortedError(Exception):
    """Run 在执行中被取消或租约失效：后续模型轮次不再开始。"""


@dataclass(slots=True)
class EvidenceLedger:
    """本 Run 的证据台账：初始上下文块 + 工具产生的证据。

    编号（``S1``、``S2``…）在整个 Run 内**单调递增且唯一**，模型只能引用台账里
    出现过的编号；重放已有步骤时按存储的编号原样登记，因此重试不会造成编号漂移。
    """

    _base: ResolvedContext
    _blocks: list[ContextBlock] = field(default_factory=list)
    _used: set[str] = field(default_factory=set)
    _next_ref: int = 1

    def __init__(self, base: ResolvedContext) -> None:
        self._base = base
        self._blocks = list(base.blocks)
        self._used = {block.ref for block in self._blocks}
        self._next_ref = len(self._blocks) + 1

    @property
    def blocks(self) -> list[ContextBlock]:
        return self._blocks

    def append(self, evidence: ToolEvidence) -> ContextBlock:
        """登记一条新证据并分配编号。"""
        ref = f"S{self._next_ref}"
        self._next_ref += 1
        return self.register(ref, evidence)

    def register(self, ref: str, evidence: ToolEvidence) -> ContextBlock:
        """按指定编号登记（幂等重放时使用）。"""
        block = ContextBlock(
            ref=ref,
            source_type=evidence.source_type,
            source_id=evidence.source_id,
            label=evidence.label,
            text=evidence.text,
            material_id=evidence.material_id,
            chunk_id=evidence.chunk_id,
            location_start=evidence.location_start,
            location_end=evidence.location_end,
            section_title=evidence.section_title,
            material_name=evidence.material_name,
            source_location_type=evidence.source_location_type,
            groundable=evidence.groundable,
            display_kind=evidence.display_kind,
        )
        self._used.add(ref)
        suffix = ref[1:] if ref.startswith("S") and ref[1:].isdigit() else None
        if suffix is not None:
            self._next_ref = max(self._next_ref, int(suffix) + 1)
        self._blocks.append(block)
        return block

    def to_context(self) -> ResolvedContext:
        """产出用于最终校验与来源快照的上下文。"""
        return ResolvedContext(
            course_id=self._base.course_id,
            entity_type=self._base.entity_type,
            entity_id=self._base.entity_id,
            summary=self._base.summary,
            blocks=list(self._blocks),
            history=self._base.history,
            truncated_note=self._base.truncated_note,
            searched_materials=self._base.searched_materials,
            searched_material_names=list(self._base.searched_material_names),
        )


@dataclass(frozen=True, slots=True)
class AgentStepRecord:
    """一条已落库的审计步骤（回传给 Worker 做日志与排错）。"""

    order: int
    kind: str
    call_id: str
    name: str
    status: str
    error_code: str | None


@dataclass(slots=True)
class AgentLoopOutcome:
    """循环的最终结果。"""

    validated: generation_ai.ValidatedAgentAnswer
    steps: list[AgentStepRecord]
    artifacts: list[AgentArtifact]
    model: str
    used_tools: list[str]
    orchestrator_version: str = ORCHESTRATOR_VERSION


# --------------------------------------------------------------------------- #
# 工具结果的边界处理
# --------------------------------------------------------------------------- #
def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _fit_payload(payload: dict, max_bytes: int) -> tuple[dict, bool]:
    """把 payload 收敛到字节上限内。

    按**条目边界**截断：先缩短每条证据的原文，再逐条删除证据，最后退化成带
    ``truncated`` 说明的摘要。任何情况下返回的都是**合法 JSON**。
    """
    if len(_dumps(payload).encode("utf-8")) <= max_bytes:
        return payload, False

    evidence = payload.get("evidence")
    if isinstance(evidence, list) and evidence:
        for limit in (800, 400, 200, 100):
            trimmed = []
            for item in evidence:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str) and len(text) > limit:
                    trimmed.append({**item, "text": text[:limit]})
                else:
                    trimmed.append(dict(item))
            candidate = {**payload, "evidence": trimmed}
            if len(_dumps(candidate).encode("utf-8")) <= max_bytes:
                return candidate, True

        kept = list(evidence)
        while kept:
            kept = kept[:-1]
            candidate = {**payload, "evidence": kept}
            if len(_dumps(candidate).encode("utf-8")) <= max_bytes:
                return candidate, True
        payload = {**payload, "evidence": []}

    # 资料/作业列表存放在 data 中；过长时保留前面的可见条目，
    # 不能把整个列表退化成一句「结果过大」，否则模型无法继续选资料。
    data = payload.get("data")
    if isinstance(data, dict):
        for key, value in data.items():
            if not isinstance(value, list) or not value:
                continue
            kept = list(value)
            while kept:
                kept.pop()
                candidate = {**payload, "data": {**data, key: kept}, "truncated": True}
                if len(_dumps(candidate).encode("utf-8")) <= max_bytes:
                    return candidate, True

    # 连去掉证据都放不下：只保留状态与错误，明确标注被截断
    fallback: dict[str, Any] = {
        "ok": payload.get("ok", False),
        "truncated": True,
        "note": "工具结果过大，已省略明细。请缩小查询范围后重试。",
    }
    if payload.get("error") is not None:
        fallback["error"] = payload["error"]
    if len(_dumps(fallback).encode("utf-8")) > max_bytes:
        fallback = {
            "ok": payload.get("ok", False),
            "truncated": True,
            "error": {"code": "RESULT_TRUNCATED", "message": "工具结果过大。"},
        }
    return fallback, True


def _model_payload(
    result: ToolResult, refs: list[str], *, drop_instructions: bool
) -> dict:
    """构造回给模型的 JSON（不含内部字段、不含数据库细节）。"""
    data = dict(result.data)
    if drop_instructions:
        # Skill 正文由 orchestrator 作为受信任指令注入，避免上下文里出现两遍
        data.pop("instructions", None)
    payload: dict[str, Any] = {
        "ok": result.ok,
        "data": data,
        "evidence": [
            {"ref": ref, "label": item.label, "text": item.text}
            for ref, item in zip(refs, result.evidence)
        ],
        "artifacts": [item.model_dump(mode="json") for item in result.artifacts],
    }
    if result.error is not None:
        payload["error"] = result.error.model_dump(mode="json")
    if result.truncated:
        payload["truncated"] = True
    return payload


def _invalid_arguments_message(exc: ValidationError) -> str:
    """把 Pydantic 校验失败收敛成**字段级**说明（不含用户数据）。"""
    parts: list[str] = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error.get("loc", ())) or "参数"
        parts.append(f"{location}: {error.get('msg', '不合法')}")
    return "参数不合法：" + "；".join(parts) if parts else "参数不合法。"


# --------------------------------------------------------------------------- #
# 步骤落库
# --------------------------------------------------------------------------- #
def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def _evidence_json(block: ContextBlock) -> dict:
    """把上下文块还原成可持久化的证据条目（``ref`` 由调用方单独放）。"""
    return {
        "source_type": block.source_type.value
        if hasattr(block.source_type, "value")
        else str(block.source_type),
        "source_id": str(block.source_id),
        "label": block.label,
        "text": block.text,
        "material_id": str(block.material_id) if block.material_id else None,
        "chunk_id": str(block.chunk_id) if block.chunk_id else None,
        "location_start": block.location_start,
        "location_end": block.location_end,
        "section_title": block.section_title,
        "material_name": block.material_name,
        "source_location_type": block.source_location_type,
        "groundable": block.groundable,
        "display_kind": block.display_kind,
    }


def _step_response_payload(result: ToolResult, blocks: list[ContextBlock]) -> dict:
    """步骤里保存的 response：结果摘要 + 带编号的证据（有界）。

    证据是**扁平结构**（``ref`` 与各字段同级），这样 :func:`_fit_payload`
    能按 ``text`` 精确裁剪，而不是整条丢弃。

    ``load_skill`` 只保留名称与版本，**不落 Skill 正文**（开发方案 7.1）：
    正文已经注入到提示词里，审计表不需要再存一份最多 32 KiB 的说明文本。
    """
    data = dict(result.data)
    if "instructions" in data:
        data.pop("instructions")
        data["instructions_loaded"] = True

    return {
        "ok": result.ok,
        "data": data,
        "artifacts": [item.model_dump(mode="json") for item in result.artifacts],
        "error": result.error.model_dump(mode="json") if result.error else None,
        "loaded_skills": list(result.loaded_skills),
        "truncated": result.truncated,
        "evidence": [{"ref": block.ref, **_evidence_json(block)} for block in blocks],
    }


def _serialize_step_response(payload: dict) -> dict:
    """步骤的 response 也必须是有界、合法的 JSON 对象。"""
    fitted, _truncated = _fit_payload(payload, STEP_PAYLOAD_MAX_BYTES)
    return fitted


async def _persist_step(
    ctx: ToolContext,
    *,
    kind: StepKind,
    call_id: str,
    name: str,
    status: StepStatus,
    request_json: dict,
    response_json: dict | None,
    error_code: str | None,
    started_at: datetime,
    finished_at: datetime,
) -> AgentStepRecord:
    """在**短事务**里写一条步骤终态记录。"""
    async with ctx.session_factory() as session:
        order = await repo.next_step_order(session, run_id=ctx.run_id)
        repo.add_step(
            session,
            step_id=uuid.uuid4(),
            run_id=ctx.run_id,
            step_order=order,
            kind=kind.value,
            call_id=_clip(call_id, STEP_CALL_ID_MAX_LENGTH),
            name=_clip(name, STEP_NAME_MAX_LENGTH),
            status=status.value,
            request_json=request_json,
            response_json=response_json,
            error_code=(
                _clip(error_code, STEP_ERROR_CODE_MAX_LENGTH) if error_code else None
            ),
            started_at=started_at,
            finished_at=finished_at,
            now=finished_at,
        )
        await session.commit()
    return AgentStepRecord(
        order=order,
        kind=kind.value,
        call_id=_clip(call_id, STEP_CALL_ID_MAX_LENGTH),
        name=_clip(name, STEP_NAME_MAX_LENGTH),
        status=status.value,
        error_code=error_code,
    )


def _idempotency_key(ctx: ToolContext, spec: ToolSpec, args: BaseModel) -> str:
    """写工具的幂等键。

    用 ``(run_id, 工具名, 规范化参数)`` 的摘要，而**不是**模型的 ``tool_call_id``：
    Worker 丢失租约重试时模型会重新生成一批 call id，若只按 call id 去重，
    同一个"生成练习"请求会被执行两次，产生两套练习。以语义请求为键才能保证
    「重试不重复创建」（开发方案 11.2）。
    """
    canonical = json.dumps(
        args.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(
        f"{ctx.run_id}|{spec.name}|{canonical}".encode("utf-8")
    ).hexdigest()
    return f"idem:{digest[:32]}"


def _replay_from_step(step: Any) -> tuple[ToolResult, list[tuple[str, ToolEvidence]]]:
    """从已成功的步骤里重放结果（不再次执行 handler）。"""
    response = step.response_json or {}
    pairs: list[tuple[str, ToolEvidence]] = []
    raw_evidence = response.get("evidence")
    if isinstance(raw_evidence, list):
        for item in raw_evidence:
            if not isinstance(item, dict):
                continue
            payload = {key: value for key, value in item.items() if key != "ref"}
            try:
                pairs.append((str(item.get("ref", "")), ToolEvidence.model_validate(payload)))
            except Exception:  # noqa: BLE001 - 老数据或被裁剪的条目直接跳过
                continue

    result = ToolResult(
        ok=bool(response.get("ok", True)),
        data=response.get("data") if isinstance(response.get("data"), dict) else {},
        artifacts=[
            AgentArtifact.model_validate(item)
            for item in (response.get("artifacts") or [])
            if isinstance(item, dict)
        ],
        error=response.get("error"),
        loaded_skills=[str(item) for item in (response.get("loaded_skills") or []) if item],
        truncated=bool(response.get("truncated")),
    )
    return result, pairs


# --------------------------------------------------------------------------- #
# 循环状态
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _LoopState:
    tool_calls_used: int = 0
    write_calls_used: int = 0
    invalid_arguments: int = 0
    result_bytes_used: int = 0
    steps: list[AgentStepRecord] = field(default_factory=list)
    artifacts: list[AgentArtifact] = field(default_factory=list)
    used_tools: list[str] = field(default_factory=list)
    loaded_skills: dict[str, str] = field(default_factory=dict)


def _loaded_skills_text(state: _LoopState) -> str:
    """已加载 Skill 的受信任指令段（只注入一次、总长受限）。"""
    if not state.loaded_skills:
        return ""
    parts = ["## 已加载的 Skill（服务端受信任指令）"]
    for name, body in state.loaded_skills.items():
        parts.append(f"### {name}\n{body}")
    return "\n\n".join(parts)


def _skills_bytes(loaded: dict[str, str]) -> int:
    return sum(len(body.encode("utf-8")) for body in loaded.values())


def _accept_loaded_skill(result: ToolResult, state: _LoopState) -> ToolResult:
    """按本 Run 的预算恢复 Skill 指令；审计与 tool message 都不携带正文。"""
    if not result.ok or not result.loaded_skills:
        return result
    skill_name = result.loaded_skills[0]
    body = str(result.data.get("instructions") or "")
    if not body:
        return ToolResult.failure(ToolErrorCode.SKILL_NOT_FOUND, "Skill 正文当前不可用。")
    if skill_name in state.loaded_skills:
        return result.model_copy(
            update={"data": {**result.data, "already_loaded": True, "instructions": ""}}
        )
    if (
        _skills_bytes(state.loaded_skills) + len(body.encode("utf-8"))
        > LOADED_SKILLS_TOTAL_MAX_BYTES
    ):
        return ToolResult.failure(
            ToolErrorCode.SKILL_BUDGET_EXCEEDED,
            "已加载的工作流说明总量已达上限，无法再加载新的 Skill。",
        )
    state.loaded_skills[skill_name] = body
    return result


def _merge_artifacts(state: _LoopState, artifacts: list[AgentArtifact]) -> None:
    for artifact in artifacts:
        if all(item.id != artifact.id for item in state.artifacts):
            state.artifacts.append(artifact)


def _build_system_message(
    *,
    ctx: ToolContext,
    registry: ToolRegistry,
    action: Any,
    skills_catalog: str,
    state: _LoopState,
) -> dict[str, Any]:
    return {
        "role": "system",
        "content": build_system_prompt(
            action,
            tools=registry.descriptions_for(ctx),
            skills_catalog=skills_catalog,
            loaded_skills=_loaded_skills_text(state),
        ),
    }


# --------------------------------------------------------------------------- #
# 主循环
# --------------------------------------------------------------------------- #
async def run_agent_loop(
    *,
    ctx: ToolContext,
    registry: ToolRegistry,
    initial_context: ResolvedContext,
    action: Any,
    question: str,
    no_evidence_message: str,
    output_language: str | None,
    skills_catalog: str = "",
    client: httpx.Client,
    base_url: str,
    api_key: str,
    model: str,
    should_stop: Callable[[], bool] | None = None,
    is_still_active: Callable[[], Awaitable[bool]] | None = None,
) -> AgentLoopOutcome:
    """执行有界的模型/工具循环。

    :param initial_context: Worker 解析出的业务上下文（首轮提示词直接使用）。
    :param client: 同步 ``httpx.Client``；每次模型调用都丢到线程池执行。
    :param should_stop: 同步停止信号（租约续期失败时由心跳置位）。
    :param is_still_active: 每轮开始前直接查一次"这个 Run 是否仍由本执行者持有"。
        返回 ``False`` 表示已被取消或被新一轮接管，循环立即停止。
        只有心跳是不够的——租约是分钟级，取消请求必须更快生效
        （开发方案 10.2：Run 执行中被取消 → 后续模型轮次不再开始）。
    :raises AgentModelNotConfiguredError: 未配置模型端点或名称。
    :raises ModelToolCallUnsupportedError: provider 不支持工具协议。
    :raises AgentLoopLimitError: 轮次或工具次数超限。
    :raises AgentToolArgumentsError: 模型连续给出非法工具参数。
    :raises AgentRunAbortedError: Run 在执行中被取消。
    :raises AgentGenerationError: 模型输出为空或不是合法 JSON。
    """
    ledger = EvidenceLedger(initial_context)
    state = _LoopState()
    messages: list[dict[str, Any]] = [
        _build_system_message(
            ctx=ctx,
            registry=registry,
            action=action,
            skills_catalog=skills_catalog,
            state=state,
        ),
        {
            "role": "user",
            "content": build_user_prompt(
                initial_context, question=question, output_language=output_language
            ),
        },
    ]

    async def ensure_not_aborted() -> None:
        if should_stop is not None and should_stop():
            raise AgentRunAbortedError()
        if is_still_active is not None and not await is_still_active():
            raise AgentRunAbortedError()

    for _round_no in range(1, MAX_MODEL_ROUNDS + 1):
        await ensure_not_aborted()
        # 每轮重建 system 消息：本轮已加载的 Skill 正文才能进入上下文
        messages[0] = _build_system_message(
            ctx=ctx,
            registry=registry,
            action=action,
            skills_catalog=skills_catalog,
            state=state,
        )

        completion = await asyncio.to_thread(
            complete,
            client=client,
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            tools=registry.schemas_for(ctx),
        )

        if not completion.has_tool_calls:
            content = (completion.content or "").strip()
            if not content:
                raise AgentGenerationError("模型没有返回任何内容")
            answer = generation_ai.parse_generated_answer(content)
            validated = generation_ai.validate_answer(
                answer,
                ledger.to_context(),
                no_evidence_message=no_evidence_message,
                # 执行过工具时，正文往往是在解释工具结果（权限拒绝、错误码、
                # 检索为空），必须保留而不是替换成"没有找到依据"
                keep_answer_without_evidence=bool(state.steps),
            )
            return AgentLoopOutcome(
                validated=validated,
                steps=list(state.steps),
                artifacts=list(state.artifacts),
                model=completion.model or model,
                used_tools=list(state.used_tools),
            )

        # 有 tool_calls 时 content 只作中间文本：不展示、不落库（开发方案 5.5）
        remaining = MAX_TOOL_CALLS - state.tool_calls_used
        if len(completion.tool_calls) > remaining:
            raise AgentLoopLimitError(
                f"工具调用次数超出上限（{ToolErrorCode.AGENT_LOOP_LIMIT.value}）："
                f"本 Run 最多 {MAX_TOOL_CALLS} 次"
            )

        # 必须把这条 assistant 消息（含 tool_calls）回填进对话：
        # Chat Completions 协议要求每个 tool 消息都紧跟对应的 assistant tool_calls，
        # 否则真实模型服务会以 400 拒绝后续请求。
        # 中间文本刻意不回填（它既不该展示，也不该影响下一轮）。
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in completion.tool_calls
                ],
            }
        )

        for call in completion.tool_calls:
            await ensure_not_aborted()
            await _handle_tool_call(
                ctx=ctx,
                registry=registry,
                call=call,
                ledger=ledger,
                state=state,
                messages=messages,
                question=question,
            )

    raise AgentLoopLimitError(
        f"模型轮次超出上限（{ToolErrorCode.AGENT_LOOP_LIMIT.value}）："
        f"本 Run 最多 {MAX_MODEL_ROUNDS} 轮"
    )


# --------------------------------------------------------------------------- #
# 单次工具调用
# --------------------------------------------------------------------------- #
def _policy_violation(
    spec: ToolSpec, ctx: ToolContext, state: _LoopState, *, question: str
) -> ToolResult | None:
    """执行前的权限与写策略判定；返回 ``None`` 表示允许执行。"""
    if not ToolRegistry.is_allowed(spec, ctx):
        return ToolResult.failure(
            ToolErrorCode.FORBIDDEN, "当前角色没有使用这个工具的权限。"
        )
    if spec.side_effect is not ToolSideEffect.WRITE:
        return None
    if state.write_calls_used >= MAX_WRITE_CALLS:
        return ToolResult.failure(
            ToolErrorCode.WRITE_LIMIT_REACHED,
            "本次对话已经执行过一次创建操作，不再重复执行。",
        )
    if not ctx.is_course_teacher:
        return ToolResult.failure(
            ToolErrorCode.FORBIDDEN, "只有这门课程的创建教师才能执行这个操作。"
        )
    if not has_write_intent(question):
        # 历史里出现过"出题"不算这次的新指令（开发方案 5.4 第 2 条）
        return ToolResult.failure(
            ToolErrorCode.WRITE_INTENT_REQUIRED,
            "用户本轮没有明确要求创建，请先向用户确认要生成什么、生成多少。",
        )
    return None


def _append_tool_message(
    messages: list[dict[str, Any]],
    call: ToolCallDraft,
    payload: dict,
    state: _LoopState,
) -> None:
    """按 ``tool_call_id`` 回传工具结果，并执行累计字节预算。"""
    remaining = max(0, TOOL_RESULTS_TOTAL_MAX_BYTES - state.result_bytes_used)
    # 为本 Run 尚可能发生的调用各留 256 字节，避免预算耗尽后额外回传
    # 512 字节，突破累计硬上限。
    reserve = (MAX_TOOL_CALLS - state.tool_calls_used) * 256
    budget = min(TOOL_RESULT_MAX_BYTES, max(256, remaining - reserve))
    fitted, truncated = _fit_payload(payload, budget)
    if truncated:
        fitted["truncated"] = True
    content = _dumps(fitted)
    state.result_bytes_used += len(content.encode("utf-8"))
    messages.append({"role": "tool", "tool_call_id": call.id, "content": content})


async def _handle_tool_call(
    *,
    ctx: ToolContext,
    registry: ToolRegistry,
    call: ToolCallDraft,
    ledger: EvidenceLedger,
    state: _LoopState,
    messages: list[dict[str, Any]],
    question: str,
) -> None:
    """执行（或重放）一次工具调用，并把结果作为 tool message 追加。"""
    state.tool_calls_used += 1
    started_at = utc_now()

    spec = registry.get(call.name)
    if spec is None:
        # 未注册工具：不执行任何东西，写失败 step 并把错误回给模型
        await _fail_call(
            ctx=ctx,
            state=state,
            messages=messages,
            call=call,
            name=call.name,
            result=ToolResult.failure(
                ToolErrorCode.UNKNOWN_TOOL,
                f"没有名为 {call.name} 的工具。只能调用本次提供的工具。",
            ),
            error_code=ToolErrorCode.UNKNOWN_TOOL.value,
            request_json={"unknown_tool": True},
            started_at=started_at,
        )
        return

    # ---------------- 参数校验（extra="forbid" 的输入模型） ---------------- #
    try:
        args = spec.input_model.model_validate_json(call.arguments or "{}")
    except ValidationError as exc:
        state.invalid_arguments += 1
        if state.invalid_arguments > MAX_INVALID_ARGUMENT_RETRIES:
            raise AgentToolArgumentsError(
                f"模型连续给出非法工具参数（{ToolErrorCode.INVALID_TOOL_ARGUMENTS.value}）"
            ) from exc
        await _fail_call(
            ctx=ctx,
            state=state,
            messages=messages,
            call=call,
            name=call.name,
            result=ToolResult.failure(
                ToolErrorCode.INVALID_TOOL_ARGUMENTS, _invalid_arguments_message(exc)
            ),
            error_code=ToolErrorCode.INVALID_TOOL_ARGUMENTS.value,
            request_json={"invalid_arguments": True},
            started_at=started_at,
        )
        return
    except Exception as exc:  # noqa: BLE001 - 非 JSON 字符串同样按参数非法处理
        state.invalid_arguments += 1
        if state.invalid_arguments > MAX_INVALID_ARGUMENT_RETRIES:
            raise AgentToolArgumentsError(
                f"模型连续给出非法工具参数（{ToolErrorCode.INVALID_TOOL_ARGUMENTS.value}）"
            ) from exc
        await _fail_call(
            ctx=ctx,
            state=state,
            messages=messages,
            call=call,
            name=call.name,
            result=ToolResult.failure(
                ToolErrorCode.INVALID_TOOL_ARGUMENTS, "参数不是合法的 JSON 对象。"
            ),
            error_code=ToolErrorCode.INVALID_TOOL_ARGUMENTS.value,
            request_json={"invalid_arguments": True},
            started_at=started_at,
        )
        return

    state.invalid_arguments = 0
    request_json = {"arguments": args.model_dump(mode="json")}

    # ------------------------- 权限与调用策略 ------------------------- #
    denial = _policy_violation(spec, ctx, state, question=question)
    if denial is not None:
        await _fail_call(
            ctx=ctx,
            state=state,
            messages=messages,
            call=call,
            name=call.name,
            result=denial,
            error_code=denial.error.code if denial.error else None,
            request_json=request_json,
            started_at=started_at,
        )
        return

    # --------------------------- 幂等检查 --------------------------- #
    step_key = (
        _idempotency_key(ctx, spec, args)
        if spec.side_effect is ToolSideEffect.WRITE
        else call.id
    )
    async with ctx.session_factory() as session:
        existing = await repo.get_step_by_call_id(
            session, run_id=ctx.run_id, call_id=step_key
        )
    if existing is not None and existing.status == StepStatus.SUCCEEDED.value:
        replayed, pairs = _replay_from_step(existing)
        if spec.name == "load_skill":
            # 审计步骤刻意不保存正文；重放时必须从受信任目录重新读取，
            # 否则下一轮 system prompt 会静默丢失已经加载的 Skill。
            if replayed.loaded_skills != [args.name]:
                fresh = ToolResult.failure(ToolErrorCode.SKILL_NOT_FOUND, "Skill 调用参数与已记录的结果不一致。")
            else:
                try:
                    fresh = await spec.handler(ctx, args)
                except Exception:  # noqa: BLE001 - 与正常工具执行相同的安全错误边界
                    logger.exception("Skill 重放读取失败（run=%s）", ctx.run_id)
                    fresh = ToolResult.failure(ToolErrorCode.INTERNAL, "Skill 当前不可用。")
            if fresh.ok and fresh.data.get("version") != replayed.data.get("version"):
                fresh = ToolResult.failure(ToolErrorCode.SKILL_NOT_FOUND, "Skill 版本已变化，请重新发起对话。")
            replayed = _accept_loaded_skill(fresh, state)
        blocks = [ledger.register(ref, item) for ref, item in pairs if ref]
        if spec.side_effect is ToolSideEffect.WRITE:
            state.write_calls_used += 1
        _merge_artifacts(state, replayed.artifacts)
        if spec.name not in state.used_tools:
            state.used_tools.append(spec.name)
        payload = _model_payload(
            replayed,
            [block.ref for block in blocks],
            drop_instructions=spec.name == "load_skill",
        )
        state.steps.append(
            AgentStepRecord(
                order=existing.step_order,
                kind=(
                    StepKind.SKILL_LOAD if spec.name == "load_skill" else StepKind.TOOL_CALL
                ).value,
                call_id=step_key,
                name=spec.name,
                status=(StepStatus.SUCCEEDED if replayed.ok else StepStatus.FAILED).value,
                error_code=replayed.error.code if replayed.error else None,
            )
        )
        _append_tool_message(messages, call, payload, state)
        logger.info(
            "工具重放（run=%s tool=%s order=%s）",
            ctx.run_id,
            spec.name,
            existing.step_order,
        )
        return

    # --------------------------- 执行 handler --------------------------- #
    try:
        result = await spec.handler(ctx, args)
    except AgentRunAbortedError:
        raise
    except Exception:  # noqa: BLE001 - 工具异常必须收敛成稳定错误码
        logger.exception("工具执行异常（run=%s tool=%s）", ctx.run_id, spec.name)
        result = ToolResult.failure(
            ToolErrorCode.INTERNAL, "工具执行失败，请稍后重试或换一种方式提问。"
        )

    if spec.name not in state.used_tools:
        state.used_tools.append(spec.name)

    blocks: list[ContextBlock] = []
    if result.ok:
        blocks = [ledger.append(item) for item in result.evidence]
        if spec.side_effect is ToolSideEffect.WRITE:
            state.write_calls_used += 1
        _merge_artifacts(state, result.artifacts)

    # ---------------------- Skill 预算与一次性注入 ---------------------- #
    if spec.name == "load_skill" and result.ok and result.loaded_skills:
        result = _accept_loaded_skill(result, state)
        if not result.ok:
            blocks = []

    payload = _model_payload(
        result,
        [block.ref for block in blocks],
        drop_instructions=spec.name == "load_skill",
    )
    await _finish_call(
        ctx=ctx,
        state=state,
        messages=messages,
        call=call,
        name=spec.name,
        result=result,
        payload=payload,
        blocks=blocks,
        status=StepStatus.SUCCEEDED if result.ok else StepStatus.FAILED,
        error_code=None if result.ok else (result.error.code if result.error else None),
        request_json=request_json,
        started_at=started_at,
        step_key=step_key,
    )


async def _fail_call(
    *,
    ctx: ToolContext,
    state: _LoopState,
    messages: list[dict[str, Any]],
    call: ToolCallDraft,
    name: str,
    result: ToolResult,
    error_code: str | None,
    request_json: dict,
    started_at: datetime,
) -> None:
    """记录一次"没有进入执行阶段"的失败调用（未知工具/参数非法/策略拒绝）。"""
    payload = _model_payload(result, [], drop_instructions=False)
    await _finish_call(
        ctx=ctx,
        state=state,
        messages=messages,
        call=call,
        name=name,
        result=result,
        payload=payload,
        blocks=[],
        status=StepStatus.FAILED,
        error_code=error_code,
        request_json=request_json,
        started_at=started_at,
        step_key=call.id,
    )


async def _finish_call(
    *,
    ctx: ToolContext,
    state: _LoopState,
    messages: list[dict[str, Any]],
    call: ToolCallDraft,
    name: str,
    result: ToolResult,
    payload: dict,
    blocks: list[ContextBlock],
    status: StepStatus,
    error_code: str | None,
    request_json: dict,
    started_at: datetime,
    step_key: str,
) -> None:
    """落步骤终态 + 回传 tool message。"""
    finished_at = utc_now()
    record = await _persist_step(
        ctx,
        kind=StepKind.SKILL_LOAD if name == "load_skill" else StepKind.TOOL_CALL,
        call_id=step_key,
        name=name,
        status=status,
        request_json=request_json,
        response_json=_serialize_step_response(_step_response_payload(result, blocks)),
        error_code=error_code,
        started_at=started_at,
        finished_at=finished_at,
    )
    state.steps.append(record)
    _append_tool_message(messages, call, payload, state)


__all__ = [
    "MAX_INVALID_ARGUMENT_RETRIES",
    "MAX_MODEL_ROUNDS",
    "MAX_TOOL_CALLS",
    "MAX_WRITE_CALLS",
    "ORCHESTRATOR_VERSION",
    "AgentLoopLimitError",
    "AgentLoopOutcome",
    "AgentRunAbortedError",
    "AgentStepRecord",
    "AgentToolArgumentsError",
    "EvidenceLedger",
    "run_agent_loop",
]
