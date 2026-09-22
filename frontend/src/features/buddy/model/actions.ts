/**
 * 页面级 AI Action 预设（DEVELOPMENT_SPEC.md 第 9.1 节）。
 *
 * ``label`` 是按钮上显示的短文案，``prompt`` 是真正送进会话的问题，
 * 两者分开是为了按钮保持轻量、而问题本身足够明确。
 *
 * ⚠️ 但并非所有按钮都该走对话。docs/local-development-agent-backend.md 第 6.9 节明确：
 * 「自动出题」已经是一个正式领域能力（PracticeSet + Job + Worker + 发布 + 答题），
 * 前端应当调用 Practice API，而**不是**在聊天消息里伪造一份题目 JSON。
 * 因此这类动作用 ``route`` 指向对应的领域页面，由那条真实流程完成。
 */

import type { AgentRunActionDto } from "../api";
import type { BuddyEntityType } from "./types";

export type AgentAction = {
  label: string;
  /** 送进会话的问题；与 ``route`` 二选一 */
  prompt?: string;
  /** 交给领域能力处理：点击后跳转到该路径（支持 {{courseId}} 占位） */
  route?: string;
  /**
   * 这次提问在后端对应的动作（``docs/local-development-agent-backend.md`` 6.3 / 6.9）。
   * 按钮不能只是"换一段提示词"：后端按 action + context 决定加载什么、用哪套模板。
   * 省略时按 ``ASK`` 处理。
   */
  agentAction?: AgentRunActionDto;
  /** 悬停说明，解释这个动作实际会做什么 */
  hint?: string;
};

export const assignmentActions: AgentAction[] = [
  {
    label: "总结要求",
    prompt: "请帮我总结这个实验任务的要求。",
    agentAction: "SUMMARIZE_CONTEXT",
  },
  {
    label: "拆解任务",
    prompt: "请帮我拆解这个实验任务，并给出完成步骤。",
    agentAction: "BREAK_DOWN_ASSIGNMENT",
  },
  { label: "易错点", prompt: "请告诉我这个实验最容易出错的地方。", agentAction: "ASK" },
];

/** 学生提交报告后才出现，且与正式「AI 批改」概念分离 */
export const submissionActions: AgentAction[] = [
  {
    label: "提交前检查",
    prompt: "请帮我检查这份实验报告是否覆盖了评分项要求。",
    agentAction: "CHECK_SUBMISSION",
  },
];

export const courseActions: AgentAction[] = [
  {
    label: "总结课程",
    prompt: "请帮我总结这门课程当前的学习内容。",
    agentAction: "SUMMARIZE_CONTEXT",
  },
  {
    label: "生成练习",
    route: "/courses/{{courseId}}/learn",
    hint: "进入练习页，用真实的练习流程从资料生成题目（不在对话里伪造题目）",
  },
];

export const gradeActions: AgentAction[] = [
  { label: "解释扣分项", prompt: "请帮我解释这次扣分的原因。", agentAction: "ASK" },
  { label: "制定改进计划", prompt: "请根据反馈帮我制定下一步的改进计划。", agentAction: "ASK" },
];

/** 首页 Buddy 大输入框下方的快捷入口 */
export const dashboardQuickActions: AgentAction[] = [
  {
    label: "总结课程",
    prompt: "帮我总结最近这门课程的学习内容。",
    agentAction: "SUMMARIZE_CONTEXT",
  },
  {
    label: "生成练习",
    route: "/courses/{{courseId}}/learn",
    hint: "进入练习页，用真实的练习流程从资料生成题目",
  },
  { label: "检查作业", prompt: "帮我看看有哪些作业快到截止时间了。", agentAction: "ASK" },
];

const actionsByEntity: Partial<Record<BuddyEntityType, AgentAction[]>> = {
  assignment: assignmentActions,
  submission: submissionActions,
  course: courseActions,
  grade: gradeActions,
};

export function agentActionsFor(entityType?: BuddyEntityType): AgentAction[] {
  if (!entityType) return courseActions;
  return actionsByEntity[entityType] ?? courseActions;
}

/** 把动作里的 {{courseId}} 换成当前课程；缺少课程上下文时返回 null */
export function resolveActionRoute(action: AgentAction, courseId?: string): string | null {
  if (!action.route) return null;
  if (!action.route.includes("{{courseId}}")) return action.route;
  if (!courseId) return null;
  return action.route.replace("{{courseId}}", courseId);
}

/** 会话空态下的建议提问 */
export const buddyQuickPrompts: string[] = [
  "帮我拆解任务",
  "解释评分标准",
  "生成完成计划",
];
