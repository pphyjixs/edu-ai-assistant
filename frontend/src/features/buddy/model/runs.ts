/**
 * BuddyContext → Agent Run 请求的映射（``docs/local-development-agent-backend.md`` 6.3）。
 *
 * 前端只发送**当前对象的标识**，不上传课件正文；后端负责鉴权、判断可见性并加载上下文。
 * 因此这里的职责很窄：把页面声明的 UI 上下文翻译成 `context` 字段。
 *
 * 映射规则：
 * - 没有具体对象，或页面在自己声明"课程"时 → **省略** `context`，表示整个课程范围的 ASK；
 * - 资料 / 资料章节 / 作业 → 对应 `entity_type` + `entity_id`（章节另带 `section_id`）；
 * - `submission` / `grade` / `practice` 尚未支持 → 同样省略 context，
 *   由后端按课程范围处理，而不是伪造一个后端不认的实体类型。
 *
 * 动作按钮会带上明确的 `action`，因此「总结这份资料」是真的 `SUMMARIZE_CONTEXT +
 * MATERIAL`，而不是把意图写在文本里让模型猜（文档 6.9）。
 */

import type {
  AgentEntityTypeDto,
  AgentRunActionDto,
  AgentRunCreateRequestDto,
} from "../api";
import type { BuddyContext } from "./types";

const ENTITY_TYPE_MAP: Partial<Record<string, AgentEntityTypeDto>> = {
  material: "MATERIAL",
  "material-section": "MATERIAL_SECTION",
  assignment: "ASSIGNMENT",
};

export type RunInput = {
  /** 用户这次说的话（动作按钮的预设文本也会写进来，便于历史展示） */
  input: string;
  action: AgentRunActionDto;
  /** 用户选中文本，最多 4000 字符；只是附加材料，不是系统指令 */
  selectedText?: string;
};

/** 生成幂等键：同一用户下唯一，网络重试会返回同一个 Run */
export function newClientRequestId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  // 兜底：非安全上下文（http 且非 localhost）没有 randomUUID
  return `run-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
}

export function buildRunRequest(
  context: BuddyContext,
  runInput: RunInput,
): AgentRunCreateRequestDto {
  const entityType = context.entityType ? ENTITY_TYPE_MAP[context.entityType] : undefined;

  // context / options 在生成的类型里是必填字段（OpenAPI 声明为可空），
  // 因此没有上下文时显式传 null——后端把它与"不传"等价处理。
  const body: AgentRunCreateRequestDto = {
    input: runInput.input,
    action: runInput.action,
    context: null,
    options: null,
    client_request_id: newClientRequestId(),
  };

  if (entityType && context.entityId) {
    body.context = {
      entity_type: entityType,
      entity_id: context.entityId,
      // 章节上下文必须带 section_id，否则后端按缺少必填字段拒绝
      section_id: entityType === "MATERIAL_SECTION" ? (context.sectionId ?? null) : null,
      selected_text:
        runInput.selectedText?.slice(0, 4000) ?? context.selectedText?.slice(0, 4000) ?? null,
    };
  }

  return body;
}

/** Run 状态 → 界面文案（PENDING/RUNNING 是"进行中"，不是失败） */
export const RUN_STATUS_LABEL: Record<string, string> = {
  PENDING: "排队中",
  RUNNING: "正在生成",
  SUCCEEDED: "已完成",
  FAILED: "生成失败",
  CANCELLED: "已取消",
};

/** 按动作用户可见的进度提示 */
export function runProgressHint(action: AgentRunActionDto): string {
  switch (action) {
    case "SUMMARIZE_CONTEXT":
      return "正在阅读当前资料并整理要点…";
    case "BREAK_DOWN_ASSIGNMENT":
      return "正在对照评分标准拆解任务…";
    case "CHECK_SUBMISSION":
      return "正在对照评分项检查提交内容…";
    default:
      return "正在查找依据并组织回答…";
  }
}
