/**
 * 提交与批改接口（契约第 9 节）。
 *
 * 几个容易踩的点，都写在这里，页面不用重复记：
 *
 * - **一位学生对同一任务只能有一份提交**：正式提交后再次初始化上传返回
 *   `409 SUBMISSION_ALREADY_EXISTS`；仍处于 `UPLOADING` 时会复用同一条提交记录
 *   并签发**新的**上传会话（旧会话作废）。
 * - 文件**只接受 PDF 与 DOCX**，`.doc` / `.pptx` 返回 `422 UPLOAD_INVALID`。
 * - 完成提交固定关联**当时的**评分规则版本；教师之后改 Rubric 不影响历史提交。
 * - **批改只由课程创建教师触发**，上传完成不会自动调用模型。
 * - AI 结果必须经教师复核才能发布；发布后不可再修改，重复发布幂等。
 * - 学生的批改详情在**发布前一律 404**；发布后 AI 原始建议分字段为 `null`
 *   （学生看到的是终稿，不是 AI 草稿）。
 * - 写接口的检查顺序是 认证 → 可见性 → 角色 → 归档 → 业务状态 → 请求结构 → 字段语义，
 *   因此带非法请求体的越权请求返回 401/403/404/409 而不是 422。
 */

import { buildQuery, http } from "@/services/http";
import type { JobStatusDto } from "@/services/jobs";
import type { Page, Schemas } from "@/types/api";

export type SubmissionStatusDto = Schemas["SubmissionStatus"];
export type SubmissionSummaryDto = Schemas["SubmissionSummarySchema"];
export type SubmissionDetailDto = Schemas["SubmissionDetailSchema"];
export type SubmissionUploadInitDto = Schemas["SubmissionUploadInitSchema"];
export type SubmissionUploadInitRequestDto = Schemas["SubmissionUploadInitRequest"];
export type GradeReviewDetailDto = Schemas["GradeReviewDetailSchema"];
export type GradeItemDetailDto = Schemas["GradeItemDetailSchema"];
export type GradeReviewUpdateRequestDto = Schemas["GradeReviewUpdateRequest"];
export type GradeReviewItemRequestDto = GradeReviewUpdateRequestDto["items"][number];
export type EvidenceSourceTypeDto = GradeItemDetailDto["evidence_source_type"];

/** 提交列表一页取多少条（后端默认 20、上限 100） */
export const SUBMISSION_PAGE_SIZE = 50;

export const gradingApi = {
  /**
   * 契约 9.4：GET /assignments/{assignment_id}/submissions
   *
   * 教师拿到全班正式提交（不含未完成的 `UPLOADING`），学生拿到本人 0–1 条。
   */
  listSubmissions(
    assignmentId: string,
    page = 1,
    pageSize = SUBMISSION_PAGE_SIZE,
    signal?: AbortSignal,
  ): Promise<Page<SubmissionSummaryDto>> {
    return http.get<Page<SubmissionSummaryDto>>(
      `/assignments/${assignmentId}/submissions${buildQuery({ page, page_size: pageSize })}`,
      { signal },
    );
  },

  /** 契约 9.5：GET /submissions/{submission_id} —— 正式提交额外带短时下载地址 */
  submission(submissionId: string, signal?: AbortSignal): Promise<SubmissionDetailDto> {
    return http.get<SubmissionDetailDto>(`/submissions/${submissionId}`, { signal });
  },

  /** 契约 9.2：POST /assignments/{assignment_id}/submissions/uploads */
  initUpload(
    assignmentId: string,
    body: SubmissionUploadInitRequestDto,
  ): Promise<SubmissionUploadInitDto> {
    return http.post<SubmissionUploadInitDto>(
      `/assignments/${assignmentId}/submissions/uploads`,
      body,
    );
  },

  /**
   * 契约 9.3：POST /assignments/{assignment_id}/submissions/uploads/{upload_id}/complete
   * 同一 upload 重复完成是幂等的（返回首次快照）。
   */
  completeUpload(assignmentId: string, uploadId: string): Promise<SubmissionDetailDto> {
    return http.post<SubmissionDetailDto>(
      `/assignments/${assignmentId}/submissions/uploads/${uploadId}/complete`,
      {},
    );
  },

  /**
   * 契约 9.6：POST /submissions/{submission_id}/grade —— 触发或重试 AI 批改。
   * 返回 `202` 与 `JobStatus`，前端按第 10 节轮询。
   */
  grade(submissionId: string): Promise<JobStatusDto> {
    return http.post<JobStatusDto>(`/submissions/${submissionId}/grade`, {});
  },

  /**
   * 契约 9.7：GET /submissions/{submission_id}/grade-review
   *
   * 教师始终可读；学生仅在该提交 `PUBLISHED` 后可读，否则 `404`；
   * 批改尚未生成时教师拿到 `409 SUBMISSION_NOT_READY`。
   */
  gradeReview(submissionId: string, signal?: AbortSignal): Promise<GradeReviewDetailDto> {
    return http.get<GradeReviewDetailDto>(
      `/submissions/${submissionId}/grade-review`,
      { signal },
    );
  },

  /**
   * 契约 9.8：PATCH /grade-reviews/{review_id}
   *
   * `items` 是**完整快照**：必须恰好覆盖该提交引用评分版本的全部评分项，
   * 缺失、多余、重复或跨版本都是 `422 VALIDATION_ERROR`。
   */
  updateGradeReview(
    reviewId: string,
    body: GradeReviewUpdateRequestDto,
  ): Promise<GradeReviewDetailDto> {
    return http.patch<GradeReviewDetailDto>(`/grade-reviews/${reviewId}`, body);
  },

  /** 契约 9.9：POST /grade-reviews/{review_id}/publish —— 未复核时 409 GRADE_NOT_REVIEWED */
  publishGradeReview(reviewId: string): Promise<GradeReviewDetailDto> {
    return http.post<GradeReviewDetailDto>(`/grade-reviews/${reviewId}/publish`, {});
  },
};
