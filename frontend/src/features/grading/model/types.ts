/**
 * 提交与批改的视图模型。
 *
 * DTO → VM 的转换点：页面只认 VM，后端枚举改名时只改这里。
 *
 * 三条容易混的规则在这里被固化下来：
 *
 * 1. **同一状态对不同角色说法不同**：`REVIEW_REQUIRED` 对教师是「待复核」，
 *    对学生是「教师复核中」——同一个枚举，两个视角，不能共用一份文案。
 * 2. **学生的 AI 原始建议分是 `null`**（契约 9.7）：发布后学生只看到终稿，
 *    因此 VM 里 `aiScore` 可空，页面不得把 `null` 显示成 0 分。
 * 3. **证据定位来自服务端**：`evidence_source_type` 由报告 MIME 决定
 *    （PDF 页码 / DOCX 段落号），前端只做格式化，不猜单位。
 */

import type { PillTone } from "@/components/Pill/Pill";
import { formatMonthDayTime } from "@/utils/datetime";
import { formatBytes } from "@/utils/format";
import { formatLocation, SOURCE_TYPE_LABEL } from "@/utils/location";

import type {
  GradeItemDetailDto,
  GradeReviewDetailDto,
  SubmissionDetailDto,
  SubmissionStatusDto,
  SubmissionSummaryDto,
} from "../api";

/* ------------------------------ 提交状态 ------------------------------ */

export type SubmissionStage =
  | "uploading"
  | "submitted"
  | "grading"
  | "reviewRequired"
  | "published"
  | "failed";

type StatusMeta = {
  stage: SubmissionStage;
  tone: PillTone;
  /** 教师看到的说法 */
  teacherLabel: string;
  /** 学生看到的说法（与学生可见性一致：不看 AI 草稿） */
  studentLabel: string;
  /** 教师接下来能做什么 */
  teacherHint: string;
  /** 学生为什么还没看到成绩 */
  studentHint: string;
};

const STATUS_MAP: Record<SubmissionStatusDto, StatusMeta> = {
  UPLOADING: {
    stage: "uploading",
    tone: "neutral",
    teacherLabel: "上传未完成",
    studentLabel: "上传未完成",
    teacherHint: "学生还在上传，草稿态不会出现在提交列表里。",
    studentHint: "报告还没有上传完成，重新选择文件可以继续。",
  },
  SUBMITTED: {
    stage: "submitted",
    tone: "info",
    teacherLabel: "待批改",
    studentLabel: "已提交",
    teacherHint: "可以触发 AI 批改；上传完成不会自动调用模型。",
    studentHint: "报告已提交，等待教师批改。",
  },
  GRADING: {
    stage: "grading",
    tone: "info",
    teacherLabel: "批改中",
    studentLabel: "批改中",
    teacherHint: "AI 正在按提交固定的评分规则版本批改。",
    studentHint: "教师已发起批改，请稍候。",
  },
  REVIEW_REQUIRED: {
    stage: "reviewRequired",
    tone: "warn",
    teacherLabel: "待复核",
    studentLabel: "教师复核中",
    teacherHint: "AI 已给出建议分，需要你复核后才能发布。",
    studentHint: "AI 批改已完成，等教师复核并发布成绩。",
  },
  PUBLISHED: {
    stage: "published",
    tone: "success",
    teacherLabel: "已发布",
    studentLabel: "已出分",
    teacherHint: "成绩已发布，不可再修改。",
    studentHint: "教师已发布成绩，可以查看评语与依据。",
  },
  FAILED: {
    stage: "failed",
    tone: "danger",
    teacherLabel: "批改失败",
    studentLabel: "批改未完成",
    teacherHint: "可以重试批改；失败不会留下半份草稿。",
    studentHint: "批改没有完成，等教师重试。",
  },
};

/** 学生是否能看到成绩（只有发布后） */
export function isScoreVisible(status: SubmissionStatusDto): boolean {
  return status === "PUBLISHED";
}

export type SubmissionVM = {
  id: string;
  assignmentId: string;
  courseId: string;
  studentId: string;
  status: SubmissionStatusDto;
  stage: SubmissionStage;
  statusLabel: string;
  statusTone: PillTone;
  statusHint: string;
  filename: string;
  contentType: string;
  /** 报告只可能是 PDF / DOCX，标签直接给「PDF」「DOCX」 */
  typeLabel: string;
  sizeLabel: string;
  isLate: boolean;
  isLateLabel: string | null;
  /** 提交固定的评分规则版本；未正式提交为 null */
  rubricVersion: number | null;
  submittedAtLabel: string | null;
  updatedAtLabel: string;
  sha256: string | null;
  /** 契约 9.5：预签名 GET，短时有效；未完成提交时为 null */
  downloadUrl: string | null;
  downloadExpiresLabel: string | null;
  /** 教师能否触发批改（SUBMITTED / FAILED；GRADING 走幂等重试也在允许范围） */
  canGrade: boolean;
  /** 教师能否复核（AI 已出结果） */
  canReview: boolean;
  /** 教师能否发布（已复核且未发布） */
  canPublish: boolean;
  /** 学生能否重新上传（尚未正式提交） */
  canUpload: boolean;
  /** 学生能否看到成绩详情 */
  scoreVisible: boolean;
};

const TYPE_LABEL: Record<string, string> = {
  "application/pdf": "PDF",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
};

function typeLabelOf(dto: SubmissionSummaryDto): string {
  return TYPE_LABEL[dto.content_type] ?? dto.filename.split(".").pop()?.toUpperCase() ?? "文件";
}

export function toSubmissionVM(
  dto: SubmissionSummaryDto | SubmissionDetailDto,
  options: { isTeacher: boolean; reviewed?: boolean; published?: boolean } = {
    isTeacher: false,
  },
): SubmissionVM {
  const meta = STATUS_MAP[dto.status];
  const submitted = dto.submitted_at !== null && dto.submitted_at !== undefined;
  const detail = "download_url" in dto ? dto : null;

  return {
    id: dto.id,
    assignmentId: dto.assignment_id,
    courseId: dto.course_id,
    studentId: dto.student_id,
    status: dto.status,
    stage: meta.stage,
    statusLabel: options.isTeacher ? meta.teacherLabel : meta.studentLabel,
    statusTone: meta.tone,
    statusHint: options.isTeacher ? meta.teacherHint : meta.studentHint,
    filename: dto.filename,
    contentType: dto.content_type,
    typeLabel: typeLabelOf(dto),
    sizeLabel: formatBytes(dto.size),
    isLate: dto.is_late,
    isLateLabel: dto.is_late ? "补交" : null,
    rubricVersion: dto.rubric_version,
    submittedAtLabel: submitted ? formatMonthDayTime(dto.submitted_at) : null,
    updatedAtLabel: formatMonthDayTime(dto.updated_at),
    sha256: detail?.sha256 ?? null,
    downloadUrl: detail?.download_url ?? null,
    downloadExpiresLabel: detail?.download_expires_at
      ? formatMonthDayTime(detail.download_expires_at)
      : null,
    // 契约 9.6：只有 SUBMITTED / FAILED 会真正新建或重试任务；
    // GRADING 走幂等分支（返回原任务），因此也允许点击，由后端兜住
    canGrade:
      options.isTeacher &&
      submitted &&
      (dto.status === "SUBMITTED" || dto.status === "FAILED" || dto.status === "GRADING"),
    canReview: options.isTeacher && dto.status === "REVIEW_REQUIRED",
    canPublish: options.isTeacher && dto.status === "REVIEW_REQUIRED" && options.reviewed === true,
    canUpload: !options.isTeacher && !submitted,
    scoreVisible: isScoreVisible(dto.status),
  };
}

/* ------------------------------ 批改结果 ------------------------------ */

export type GradeItemVM = {
  id: string;
  rubricItemId: string;
  order: number;
  title: string;
  maxScore: number;
  /** AI 建议分；学生视角为 null（契约 9.7） */
  aiScore: number | null;
  finalScore: number;
  aiComment: string;
  evidenceQuote: string;
  evidenceSourceType: string | null;
  /** 「P3」「段落 12-14」；服务端没给定位时为空字符串 */
  evidenceLocationLabel: string;
  errorType: string;
  improvementSuggestion: string;
  teacherComment: string;
  /** 教师改分相对 AI 建议的差值；只对学生不可见时无意义，故仅教师视角计算 */
  aiDelta: number | null;
};

export type GradeReviewVM = {
  id: string;
  submissionId: string;
  aiSummary: string;
  teacherSummary: string;
  suggestedTotalScore: number | null;
  finalTotalScore: number;
  items: GradeItemVM[];
  reviewedBy: string | null;
  reviewedAtLabel: string | null;
  publishedAtLabel: string | null;
  isReviewed: boolean;
  isPublished: boolean;
  /** 各评分项满分之和，用于展示 xxx / yyy */
  maxTotal: number;
  /** 教师终稿与 AI 建议的总分差（教师视角） */
  totalDelta: number | null;
};

function toGradeItemVM(dto: GradeItemDetailDto): GradeItemVM {
  const aiScore = dto.ai_score ?? null;
  return {
    id: dto.id,
    rubricItemId: dto.rubric_item_id,
    order: dto.order,
    title: dto.title,
    maxScore: dto.max_score,
    aiScore,
    finalScore: dto.final_score,
    aiComment: dto.ai_comment ?? "",
    evidenceQuote: dto.evidence_quote ?? "",
    evidenceSourceType: dto.evidence_source_type ?? null,
    evidenceLocationLabel: formatLocation(
      dto.evidence_source_type,
      dto.evidence_location_start,
      dto.evidence_location_end,
    ),
    errorType: dto.error_type ?? "",
    improvementSuggestion: dto.improvement_suggestion ?? "",
    teacherComment: dto.teacher_comment ?? "",
    aiDelta: aiScore === null ? null : dto.final_score - aiScore,
  };
}

export function toGradeReviewVM(dto: GradeReviewDetailDto): GradeReviewVM {
  const items = [...dto.items].sort((a, b) => a.order - b.order).map(toGradeItemVM);
  const suggested = dto.suggested_total_score ?? null;

  return {
    id: dto.id,
    submissionId: dto.submission_id,
    aiSummary: dto.ai_summary ?? "",
    teacherSummary: dto.teacher_summary ?? "",
    suggestedTotalScore: suggested,
    finalTotalScore: dto.final_total_score,
    items,
    reviewedBy: dto.reviewed_by ?? null,
    reviewedAtLabel: dto.reviewed_at ? formatMonthDayTime(dto.reviewed_at) : null,
    publishedAtLabel: dto.published_at ? formatMonthDayTime(dto.published_at) : null,
    isReviewed: Boolean(dto.reviewed_at),
    isPublished: Boolean(dto.published_at),
    maxTotal: items.reduce((sum, item) => sum + item.maxScore, 0),
    totalDelta: suggested === null ? null : dto.final_total_score - suggested,
  };
}

/** 证据来源类型 → 「PDF 页码」「段落」；用于在证据旁标注定位口径 */
export function evidenceSourceLabel(sourceType: string | null): string {
  if (!sourceType) return "报告原文";
  return SOURCE_TYPE_LABEL[sourceType] ?? "报告原文";
}
