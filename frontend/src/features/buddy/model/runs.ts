/**
 * BuddyContext → Agent Run 请求的映射（``docs/local-development-agent-backend.md`` 6.3）。
 *
 * 前端只发送**当前对象的标识**，不上传课件正文；后端负责鉴权、判断可见性并加载上下文。
 * 因此这里的职责很窄：把页面声明的 UI 上下文翻译成 `context` 字段。
 *
 * 映射规则：
 * - 没有具体对象，或页面在自己声明"课程"时 → **省略** `context`，表示整个课程范围的 ASK；
 * - 资料 / 资料章节 / 作业 → 对应 `entity_type` + `entity_id`（章节另带 `section_id`）；
 * - 页面声明了后端还不支持的上下文（practice / submission / grade）→ **直接报错**，
 *   不静默降级成 COURSE（评审文档「一、#3」）：否则用户以为在问这份提交，
 *   实际问的是整门课程，得到的回答与预期不符还看不出原因。
 *
 * 动作按钮会带上明确的 `action`，因此「总结这份资料」是真的 `SUMMARIZE_CONTEXT +
 * MATERIAL`，而不是把意图写在文本里让模型猜（文档 6.9）。
 */

import { HttpError } from "@/services/http";

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
  course: "COURSE",
};

/** 页面可能声明、但后端尚未实现对应上下文解析的类型 */
const UNSUPPORTED_CONTEXT_LABEL: Partial<Record<string, string>> = {
  practice: "练习",
  submission: "提交内容",
  grade: "成绩",
};

export type RunInput = {
  /** 用户这次说的话（动作按钮的预设文本也会写进来，便于历史展示） */
  input: string;
  action: AgentRunActionDto;
  /** 用户选中文本，最多 4000 字符；只是附加材料，不是系统指令 */
  selectedText?: string;
  selectedSkillId?: string;
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
  const declared = context.entityType;
  const unsupported = declared ? UNSUPPORTED_CONTEXT_LABEL[declared] : undefined;
  if (unsupported) {
    // 明确失败好过静默问错对象：让用户看到"这个对象暂时不能问"，
    // 而不是拿到一段针对整门课程的回答还以为是针对当前对象的
    throw new HttpError({
      code: "AGENT_CONTEXT_UNSUPPORTED",
      message: `Buddy 暂时还不能围绕${unsupported}提问，可以先回到课程页面提问。`,
      status: 422,
    });
  }

  const entityType = declared ? ENTITY_TYPE_MAP[declared] : undefined;

  // context / options 在生成的类型里是必填字段（OpenAPI 声明为可空），
  // 因此没有上下文时显式传 null——后端把它与"不传"等价处理。
  const body: AgentRunCreateRequestDto = {
    input: runInput.input,
    action: runInput.action,
    context: null,
    options: null,
    client_request_id: newClientRequestId(),
  };
  if (runInput.selectedSkillId) {
    const isSystem = runInput.selectedSkillId.startsWith("system:");
    body.options = {
      output_language: null,
      selected_skill_ids: isSystem ? [] : [runInput.selectedSkillId.replace(/^user:/, "")],
      selected_skill_names: isSystem ? [runInput.selectedSkillId.slice(7)] : [],
    } as AgentRunCreateRequestDto["options"];
  }

  // COURSE 与"没有上下文"等价：整门课程的 ASK，不需要显式传 COURSE
  if (entityType === "COURSE") return body;

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
