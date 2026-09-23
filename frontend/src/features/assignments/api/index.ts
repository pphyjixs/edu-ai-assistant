/**
 * 实验任务接口（契约第 8 节）。
 *
 * 几个容易踩的点：
 * - 状态机是 创建→DRAFT→PUBLISHED→CLOSED，截止时间**不会**自动把状态改成 CLOSED；
 * - 学生只能读到 `PUBLISHED`/`CLOSED`/`ARCHIVED`，草稿对其返回 404；
 * - 各评分项 `max_score` 之和必须**精确等于** `total_score`，否则 422 `RUBRIC_SCORE_MISMATCH`；
 * - `rubric_items` 是**完整替换**；改标题/说明/截止时间不会产生新的评分规则版本；
 * - 写接口的检查顺序是 认证→可见性→角色→归档→状态→字段→总分匹配，因此越权请求不会返回 422。
 */

import { buildQuery, http } from "@/services/http";
import type { Page, Schemas } from "@/types/api";

export type AssignmentSummaryDto = Schemas["AssignmentSummarySchema"];
export type AssignmentDetailDto = Schemas["AssignmentDetailSchema"];
export type AssignmentStatusDto = Schemas["AssignmentStatus"];
export type RubricItemDto = Schemas["RubricItemSchema"];
export type RubricItemRequestDto = Schemas["RubricItemRequest"];
export type AssignmentCreateRequestDto = Schemas["AssignmentCreateRequest"];
export type AssignmentUpdateRequestDto = Schemas["AssignmentUpdateRequest"];
export type AssignmentAttachmentDto = Schemas["AssignmentAttachmentSchema"];
export type AttachmentUploadInitRequestDto = Schemas["AttachmentUploadInitRequest"];
export type AttachmentUploadInitResponseDto = Schemas["AttachmentUploadInitResponse"];

export const assignmentsApi = {
  /**
   * 契约 8.3：GET /courses/{course_id}/assignments
   * 教师看到全部状态；学生的草稿在 SQL 层就被排除。
   */
  list(courseId: string, page = 1, pageSize = 100): Promise<Page<AssignmentSummaryDto>> {
    return http.get<Page<AssignmentSummaryDto>>(
      `/courses/${courseId}/assignments${buildQuery({ page, page_size: pageSize })}`,
    );
  },

  /** 契约 8.4：GET /assignments/{assignment_id} */
  detail(assignmentId: string, signal?: AbortSignal): Promise<AssignmentDetailDto> {
    return http.get<AssignmentDetailDto>(`/assignments/${assignmentId}`, { signal });
  },

  /** 契约 8.2：POST /courses/{course_id}/assignments —— 仅课程创建教师 */
  create(courseId: string, body: AssignmentCreateRequestDto): Promise<AssignmentDetailDto> {
    return http.post<AssignmentDetailDto>(`/courses/${courseId}/assignments`, body);
  },

  /**
   * 契约 8.5：PATCH /assignments/{assignment_id}
   * 空对象会被拒绝；`due_at: null` 表示清除截止时间（与「省略」不同）。
   */
  update(
    assignmentId: string,
    body: AssignmentUpdateRequestDto,
  ): Promise<AssignmentDetailDto> {
    return http.patch<AssignmentDetailDto>(`/assignments/${assignmentId}`, body);
  },

  /** 契约 8.6：POST /assignments/{assignment_id}/publish —— DRAFT→PUBLISHED，重复发布幂等 */
  publish(assignmentId: string): Promise<AssignmentDetailDto> {
    return http.post<AssignmentDetailDto>(`/assignments/${assignmentId}/publish`, {});
  },

  /** 契约 8.7：POST /assignments/{assignment_id}/close —— PUBLISHED→CLOSED，草稿不能关闭 */
  close(assignmentId: string): Promise<AssignmentDetailDto> {
    return http.post<AssignmentDetailDto>(`/assignments/${assignmentId}/close`, {});
  },

  /**
   * 契约 8.16：POST /assignments/{assignment_id}/reopen —— CLOSED→PUBLISHED。
   *
   * 关闭只是停止收作业，不是把任务作废，因此允许重新开启；
   * 重新开启清除 `closed_at`，但保留首次 `published_at`，
   * 也不会产生新的评分规则版本、不会清除已有提交。
   */
  reopen(assignmentId: string): Promise<AssignmentDetailDto> {
    return http.post<AssignmentDetailDto>(`/assignments/${assignmentId}/reopen`, {});
  },

  /* ------------------------- 8.15 作业附件 ------------------------- */

  /** 契约 8.15：GET /assignments/{assignment_id}/attachments —— 课程成员可读 */
  attachments(
    assignmentId: string,
    signal?: AbortSignal,
  ): Promise<AssignmentAttachmentDto[]> {
    return http.get<AssignmentAttachmentDto[]>(
      `/assignments/${assignmentId}/attachments`,
      { signal },
    );
  },

  /** 契约 8.15：POST /assignments/{assignment_id}/attachments/uploads */
  initAttachmentUpload(
    assignmentId: string,
    body: AttachmentUploadInitRequestDto,
  ): Promise<AttachmentUploadInitResponseDto> {
    return http.post<AttachmentUploadInitResponseDto>(
      `/assignments/${assignmentId}/attachments/uploads`,
      body,
    );
  },

  /** 契约 8.15：完成附件上传；同一 upload_id 幂等 */
  completeAttachmentUpload(
    assignmentId: string,
    uploadId: string,
  ): Promise<AssignmentAttachmentDto> {
    return http.post<AssignmentAttachmentDto>(
      `/assignments/${assignmentId}/attachments/uploads/${uploadId}/complete`,
      {},
    );
  },

  /** 契约 8.15：DELETE /assignments/{assignment_id}/attachments/{attachment_id} */
  deleteAttachment(assignmentId: string, attachmentId: string): Promise<void> {
    return http.delete<void>(
      `/assignments/${assignmentId}/attachments/${attachmentId}`,
    );
  },
};
