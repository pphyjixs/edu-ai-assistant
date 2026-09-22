/**
 * 实验任务的视图模型。
 *
 * DTO → VM 的转换点：页面只认 VM，后端枚举改名时只改这里。
 * 契约 8.1：状态机只由发布/关闭接口推进，截止时间**不会**把状态改成 CLOSED，
 * 因此「已截止」是时间事实，「已关闭」才是状态事实，两者在 UI 上分开表达。
 */

import type { PillTone } from "@/components/Pill/Pill";
import {
  formatDeadline,
  formatMonthDayTime,
  formatRemaining,
  type Remaining,
} from "@/utils/datetime";

import type {
  AssignmentDetailDto,
  AssignmentStatusDto,
  AssignmentSummaryDto,
  RubricItemDto,
  RubricItemRequestDto,
} from "../api";

export type AssignmentStatus = "draft" | "published" | "closed" | "archived";

const STATUS_MAP: Record<
  AssignmentStatusDto,
  { status: AssignmentStatus; label: string; tone: PillTone }
> = {
  DRAFT: { status: "draft", label: "草稿", tone: "neutral" },
  PUBLISHED: { status: "published", label: "进行中", tone: "info" },
  CLOSED: { status: "closed", label: "已关闭", tone: "neutral" },
  ARCHIVED: { status: "archived", label: "已归档", tone: "neutral" },
};

export type RubricItemVM = {
  id: string;
  title: string;
  description: string;
  maxScore: number;
  order: number;
};

export type AssignmentVM = {
  id: string;
  courseId: string;
  title: string;
  description: string;
  totalScore: number;
  dueAt: string | null;
  dueLabel: string;
  remaining: Remaining;
  status: AssignmentStatus;
  statusLabel: string;
  statusTone: PillTone;
  allowLateSubmission: boolean;
  rubric: RubricItemVM[];
  /** 当前评分规则版本号；改标题等非评分字段不会让它增长（契约 8.9） */
  rubricVersion: number;
  publishedAtLabel: string | null;
  closedAtLabel: string | null;
  updatedAtLabel: string;
  /** 评分项合计，用于提交前暴露 RUBRIC_SCORE_MISMATCH，而不是等后端 422 */
  rubricScoreSum: number;
  rubricMismatch: boolean;
  /** 契约 8.10 的可提交判断；手工关闭优先于允许补交 */
  canSubmit: boolean;
  /** 仅教师：DRAFT 与 PUBLISHED 可修改，CLOSED/ARCHIVED 不可（契约 8.1） */
  canEdit: boolean;
};

function toRubricItemVM(dto: RubricItemDto): RubricItemVM {
  return {
    id: dto.id,
    title: dto.title,
    description: dto.description,
    maxScore: dto.max_score,
    order: dto.order,
  };
}

export function toAssignmentVM(dto: AssignmentDetailDto | AssignmentSummaryDto): AssignmentVM {
  const mapped = STATUS_MAP[dto.status];
  const remaining = formatRemaining(dto.due_at);
  const rubric = "rubric_items" in dto
    ? [...dto.rubric_items].sort((a, b) => a.order - b.order).map(toRubricItemVM)
    : [];
  const rubricScoreSum = rubric.reduce((sum, item) => sum + item.maxScore, 0);

  // 契约 8.10：status == PUBLISHED 且（无截止时间 或 未到截止 或 允许补交）
  const canSubmit =
    mapped.status === "published" && (!remaining.expired || dto.allow_late_submission);

  return {
    id: dto.id,
    courseId: dto.course_id,
    title: dto.title,
    description: "description" in dto ? dto.description : "",
    totalScore: dto.total_score,
    dueAt: dto.due_at,
    dueLabel: formatDeadline(dto.due_at),
    remaining,
    status: mapped.status,
    statusLabel: mapped.label,
    statusTone: mapped.tone,
    allowLateSubmission: dto.allow_late_submission,
    rubric,
    rubricVersion: dto.rubric_version,
    publishedAtLabel: dto.published_at ? formatMonthDayTime(dto.published_at) : null,
    closedAtLabel: dto.closed_at ? formatMonthDayTime(dto.closed_at) : null,
    updatedAtLabel: formatMonthDayTime(dto.updated_at),
    rubricScoreSum,
    rubricMismatch: rubric.length > 0 && Math.abs(rubricScoreSum - dto.total_score) > 1e-9,
    canSubmit,
    canEdit: mapped.status === "draft" || mapped.status === "published",
  };
}

/* ---------------------------- 表单相关 ---------------------------- */

export type RubricDraftItem = {
  /** 新建时为本地临时键；编辑已有项时为后端 id */
  key: string;
  title: string;
  description: string;
  maxScore: string;
};

export function toRubricDraft(items: RubricItemVM[]): RubricDraftItem[] {
  return items.map((item) => ({
    key: item.id,
    title: item.title,
    description: item.description,
    maxScore: String(item.maxScore),
  }));
}

/** 把表单里的评分项转成请求体；order 由顺序决定，从 1 开始连续 */
export function toRubricRequest(items: RubricDraftItem[]): RubricItemRequestDto[] {
  return items.map((item, index) => ({
    title: item.title.trim(),
    description: item.description.trim(),
    max_score: Number(item.maxScore),
    order: index + 1,
  }));
}

export function rubricDraftSum(items: RubricDraftItem[]): number {
  return items.reduce((sum, item) => {
    const value = Number(item.maxScore);
    return sum + (Number.isFinite(value) ? value : 0);
  }, 0);
}
