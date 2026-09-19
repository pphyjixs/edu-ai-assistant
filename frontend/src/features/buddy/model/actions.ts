/**
 * 页面级 AI Action 预设（DEVELOPMENT_SPEC.md 第 9.1 节）。
 *
 * ``label`` 是按钮上显示的短文案，``prompt`` 是真正送进会话的问题，
 * 两者分开是为了按钮保持轻量、而问题本身足够明确。
 */

import type { BuddyEntityType } from "./types";

export type AgentAction = {
  label: string;
  prompt: string;
};

export const assignmentActions: AgentAction[] = [
  { label: "总结要求", prompt: "请帮我总结这个实验任务的要求。" },
  { label: "拆解任务", prompt: "请帮我拆解这个实验任务，并给出完成步骤。" },
  { label: "易错点", prompt: "请告诉我这个实验最容易出错的地方。" },
];

/** 学生提交报告后才出现，且与正式「AI 批改」概念分离 */
export const submissionActions: AgentAction[] = [
  { label: "提交前检查", prompt: "请帮我检查这份实验报告是否覆盖了评分项要求。" },
];

export const courseActions: AgentAction[] = [
  { label: "总结课程", prompt: "请帮我总结这门课程当前的学习内容。" },
  { label: "生成练习", prompt: "请基于这门课程的资料生成一组练习。" },
];

export const gradeActions: AgentAction[] = [
  { label: "解释扣分项", prompt: "请帮我解释这次扣分的原因。" },
  { label: "制定改进计划", prompt: "请根据反馈帮我制定下一步的改进计划。" },
];

/** 首页 Buddy 大输入框下方的快捷入口 */
export const dashboardQuickActions: AgentAction[] = [
  { label: "总结课程", prompt: "帮我总结最近这门课程的学习内容。" },
  { label: "生成练习", prompt: "帮我基于最近的课程资料生成一组练习。" },
  { label: "检查作业", prompt: "帮我检查一下还没完成的作业。" },
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

/** 会话空态下的建议提问 */
export const buddyQuickPrompts: string[] = [
  "帮我拆解任务",
  "解释评分标准",
  "生成完成计划",
];
