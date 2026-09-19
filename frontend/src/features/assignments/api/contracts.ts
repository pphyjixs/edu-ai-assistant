/**
 * Assignments（实验任务）与 Submission 的 DTO 定义。
 *
 * 端点按 docs/api-contract.md 第 8、9 节；字段按契约给出的创建请求
 * （title / description / total_score / due_at / allow_late_submission /
 * rubric_items）与 docs/modules.md 第 6 节的状态机。
 *
 * 后端 assignments / grading 模块尚未实现，本阶段由 mock adapter 提供数据。
 */

export type AssignmentStatusDto = "DRAFT" | "PUBLISHED" | "CLOSED" | "ARCHIVED";

export type RubricItemDto = {
  id: string;
  title: string;
  description: string;
  max_score: number;
  order: number;
};

export type AssignmentDto = {
  id: string;
  course_id: string;
  title: string;
  description: string;
  total_score: number;
  due_at: string | null;
  allow_late_submission: boolean;
  status: AssignmentStatusDto;
  rubric_items: RubricItemDto[];
  created_at: string;
};

/** 契约 9 / docs/modules.md 第 7 节的提交状态 */
export type SubmissionStatusDto =
  | "UPLOADING"
  | "SUBMITTED"
  | "GRADING"
  | "REVIEW_REQUIRED"
  | "PUBLISHED"
  | "FAILED";

export type SubmissionDto = {
  id: string;
  assignment_id: string;
  user_id: string;
  status: SubmissionStatusDto;
  created_at: string;
};

export type CreateSubmissionBody = {
  filename: string;
  size: number;
};

export interface AssignmentsApi {
  /** 契约 8：GET /courses/{course_id}/assignments */
  listAssignments(courseId: string): Promise<AssignmentDto[]>;
  /** 契约 8：GET /assignments/{assignment_id} */
  getAssignment(assignmentId: string): Promise<AssignmentDto>;
  /** 契约 9：GET /assignments/{assignment_id}/submissions 中属于本人的那一条 */
  getMySubmission(assignmentId: string): Promise<SubmissionDto | null>;
  /**
   * 提交实验报告。
   * 真实流程是契约 9 的三段式：初始化上传 → 浏览器直传对象存储 → 完成提交。
   * mock 直接返回 SUBMITTED，等 services/upload 与后端接口就绪后替换实现。
   */
  createSubmission(
    assignmentId: string,
    body: CreateSubmissionBody,
  ): Promise<SubmissionDto>;
}
