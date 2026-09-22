/**
 * 练习接口（契约第 7 节）。
 *
 * 权限与可见性是这里的重点：
 * - 生成 / 发布 / 重试：仅课程创建教师；提交：仅课程学生；
 * - 练习详情：教师看全部状态，学生只看 `PUBLISHED`（其余状态按不存在处理）；
 * - 学生响应里不含 `correct_answer` / `grading_points` / `explanation`，
 *   这些只能在提交成功后的**答题结果**里看到；
 * - 每名学生对每套练习只能提交一次（重复提交 409 PRACTICE_ALREADY_ATTEMPTED）。
 */

import { buildQuery, http } from "@/services/http";
import type { JobStatusDto } from "@/services/jobs";
import type { Page, Schemas } from "@/types/api";

export type PracticeSetSummaryDto = Schemas["PracticeSetSummarySchema"];
export type PracticeSetDto = Schemas["PracticeSetSchema"];
export type PracticeQuestionDto = Schemas["PracticeQuestionSchema"];
export type PracticeOptionDto = Schemas["PracticeOptionSchema"];
export type PracticeGradingPointDto = Schemas["PracticeGradingPointSchema"];
export type PracticeAttemptAnswerDto = Schemas["PracticeAttemptAnswerSchema"];
export type PracticeStatusDto = Schemas["PracticeStatus"];
export type PracticeDifficultyDto = Schemas["PracticeDifficulty"];
export type PracticeQuestionTypeDto = Schemas["PracticeQuestionType"];
export type PracticeGenerateRequestDto = Schemas["PracticeGenerateRequest"];
export type PracticeAttemptResultDto = Schemas["PracticeAttemptResultSchema"];
export type PracticeAttemptSubmitRequestDto = Schemas["PracticeAttemptSubmitRequest"];

export const practiceApi = {
  /**
   * 契约 7.2：POST /courses/{course_id}/practice-sets/generate
   * 返回 202 与 PRACTICE_GENERATE 任务；题目由独立 Worker 生成。
   */
  generate(courseId: string, body: PracticeGenerateRequestDto): Promise<JobStatusDto> {
    return http.post<JobStatusDto>(`/courses/${courseId}/practice-sets/generate`, body);
  },

  /** 契约 7.3：GET /courses/{course_id}/practice-sets —— 只列出已发布 */
  list(courseId: string, page = 1, pageSize = 50): Promise<Page<PracticeSetSummaryDto>> {
    return http.get<Page<PracticeSetSummaryDto>>(
      `/courses/${courseId}/practice-sets${buildQuery({ page, page_size: pageSize })}`,
    );
  },

  /** 契约 7.4：GET /practice-sets/{set_id} */
  detail(setId: string, signal?: AbortSignal): Promise<PracticeSetDto> {
    return http.get<PracticeSetDto>(`/practice-sets/${setId}`, { signal });
  },

  /** 契约 7.5：POST /practice-sets/{set_id}/publish —— DRAFT → PUBLISHED，幂等 */
  publish(setId: string): Promise<PracticeSetDto> {
    return http.post<PracticeSetDto>(`/practice-sets/${setId}/publish`, {});
  },

  /** 契约 7.6：POST /practice-sets/{set_id}/attempts */
  submit(
    setId: string,
    body: PracticeAttemptSubmitRequestDto,
  ): Promise<PracticeAttemptResultDto> {
    return http.post<PracticeAttemptResultDto>(`/practice-sets/${setId}/attempts`, body);
  },

  /** 契约 7.7：GET /practice-attempts/{attempt_id} —— 仅本人或课程创建教师 */
  attempt(attemptId: string, signal?: AbortSignal): Promise<PracticeAttemptResultDto> {
    return http.get<PracticeAttemptResultDto>(`/practice-attempts/${attemptId}`, { signal });
  },
};
