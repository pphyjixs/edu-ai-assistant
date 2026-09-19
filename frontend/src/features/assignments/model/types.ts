/**
 * 作业与提交的视图模型。
 *
 * DTO → VM 的转换点。后端用的是大写状态枚举（PUBLISHED / REVIEW_REQUIRED…），
 * UI 用的是小写可读状态，两者在这里对齐——页面不再感知后端枚举名。
 */

import type { PillTone } from "@/components/Pill/Pill";
import { formatDeadline, formatMonthDayTime, formatRemaining, type Remaining } from "@/utils/datetime";

import type { AssignmentDto, RubricItemDto, SubmissionDto, SubmissionStatusDto } from "../api";

export type AssignmentStatus = "draft" | "active" | "closed" | "archived";

const STATUS_MAP: Record<AssignmentDto["status"], { status: AssignmentStatus; label: string; tone: PillTone }> = {
  DRAFT: { status: "draft", label: "草稿", tone: "neutral" },
  PUBLISHED: { status: "active", label: "进行中", tone: "info" },
  CLOSED: { status: "closed", label: "已截止", tone: "neutral" },
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
  /** 评分项合计，用于前端提前暴露 RUBRIC_SCORE_MISMATCH，而不是等后端 422 */
  rubricScoreSum: number;
  rubricMismatch: boolean;
  /** 仅用于控制按钮可用性；最终以后端权限与状态判断为准 */
  canSubmit: boolean;
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

export function toAssignmentVM(dto: AssignmentDto): AssignmentVM {
  const mapped = STATUS_MAP[dto.status];
  const remaining = formatRemaining(dto.due_at);
  const rubric = [...dto.rubric_items]
    .sort((a, b) => a.order - b.order)
    .map(toRubricItemVM);
  const rubricScoreSum = rubric.reduce((sum, item) => sum + item.maxScore, 0);

  const canSubmit =
    mapped.status === "active" && (!remaining.expired || dto.allow_late_submission);

  return {
    id: dto.id,
    courseId: dto.course_id,
    title: dto.title,
    description: dto.description,
    totalScore: dto.total_score,
    dueAt: dto.due_at,
    dueLabel: formatDeadline(dto.due_at),
    remaining,
    status: mapped.status,
    statusLabel: mapped.label,
    statusTone: mapped.tone,
    allowLateSubmission: dto.allow_late_submission,
    rubric,
    rubricScoreSum,
    rubricMismatch: rubricScoreSum !== dto.total_score,
    canSubmit,
  };
}

export type SubmissionStatus =
  | "uploading"
  | "submitted"
  | "grading"
  | "reviewRequired"
  | "published"
  | "failed";

const SUBMISSION_MAP: Record<SubmissionStatusDto, { status: SubmissionStatus; label: string; tone: PillTone }> = {
  UPLOADING: { status: "uploading", label: "上传中", tone: "neutral" },
  SUBMITTED: { status: "submitted", label: "已提交", tone: "info" },
  GRADING: { status: "grading", label: "AI 批改中", tone: "agent" },
  REVIEW_REQUIRED: { status: "reviewRequired", label: "等待教师复核", tone: "warn" },
  PUBLISHED: { status: "published", label: "已发布成绩", tone: "success" },
  FAILED: { status: "failed", label: "批改失败", tone: "danger" },
};

export type SubmissionVM = {
  id: string;
  status: SubmissionStatus;
  statusLabel: string;
  statusTone: PillTone;
  submittedAtLabel: string;
  /** 只有教师发布后，学生看到的才是正式成绩 */
  isFinal: boolean;
};

export function toSubmissionVM(dto: SubmissionDto): SubmissionVM {
  const mapped = SUBMISSION_MAP[dto.status];
  return {
    id: dto.id,
    status: mapped.status,
    statusLabel: mapped.label,
    statusTone: mapped.tone,
    submittedAtLabel: formatMonthDayTime(dto.created_at),
    isFinal: mapped.status === "published",
  };
}
