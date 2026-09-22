/**
 * 课程接口（契约第 3 节）。
 *
 * 契约里几个容易踩的点，这里如实反映：
 * - `CourseSummary` / `CourseDetail` **没有教师对象**，只有 `teacher_id`，
 *   因此界面不显示教师姓名（成员列表里才有 display_name）；
 * - 邀请码只在「创建教师查看未归档课程」与「重置邀请码」的响应里出现，
 *   其他情况字段不存在（不是 null）；
 * - `/courses` 与成员列表都是分页的，没有「一次返回全部」的用法。
 */

import { buildQuery, http } from "@/services/http";
import type { Page, Schemas } from "@/types/api";

export type CourseSummaryDto = Schemas["CourseSummary"];
export type CourseDetailDto = Schemas["CourseDetail"];
export type CourseDetailWithInviteCodeDto = Schemas["CourseDetailWithInviteCode"];
export type CourseMemberSummaryDto = Schemas["CourseMemberSummary"];
export type CourseCreateRequestDto = Schemas["CourseCreateRequest"];
export type CourseUpdateRequestDto = Schemas["CourseUpdateRequest"];
export type CourseStatusDto = Schemas["CourseStatus"];

/** 详情响应是二选一：创建教师看未归档课程时才带邀请码 */
export type CourseDetailResponseDto = CourseDetailDto | CourseDetailWithInviteCodeDto;

export function hasInviteCode(dto: CourseDetailResponseDto): dto is CourseDetailWithInviteCodeDto {
  return typeof (dto as CourseDetailWithInviteCodeDto).invite_code === "string";
}

export const coursesApi = {
  /** 契约 3：GET /courses —— 我参加的课程（含归档），分页 */
  list(page = 1, pageSize = 20): Promise<Page<CourseSummaryDto>> {
    return http.get<Page<CourseSummaryDto>>(
      `/courses${buildQuery({ page, page_size: pageSize })}`,
    );
  },

  /** 契约 3：GET /courses/{course_id} */
  get(courseId: string, signal?: AbortSignal): Promise<CourseDetailResponseDto> {
    return http.get<CourseDetailResponseDto>(`/courses/${courseId}`, { signal });
  },

  /** 契约 3：POST /courses —— 仅教师 */
  create(body: CourseCreateRequestDto): Promise<CourseDetailWithInviteCodeDto> {
    return http.post<CourseDetailWithInviteCodeDto>("/courses", body);
  },

  /** 契约 3：PATCH /courses/{course_id} —— 仅创建教师；至少提供一个字段 */
  update(courseId: string, body: CourseUpdateRequestDto): Promise<CourseDetailWithInviteCodeDto> {
    return http.patch<CourseDetailWithInviteCodeDto>(`/courses/${courseId}`, body);
  },

  /** 契约 3：POST /courses/{course_id}/archive —— 重复归档返回 200，幂等 */
  archive(courseId: string): Promise<CourseDetailDto> {
    return http.post<CourseDetailDto>(`/courses/${courseId}/archive`, {});
  },

  /** 契约 3：POST /courses/{course_id}/invite-code —— 重置后旧码立即失效 */
  regenerateInviteCode(courseId: string): Promise<Schemas["InviteCodeResponse"]> {
    return http.post<Schemas["InviteCodeResponse"]>(`/courses/${courseId}/invite-code`, {});
  },

  /**
   * 契约 3：POST /courses/join —— 仅学生。
   * 首次加入返回 201，已是成员返回 200；两者响应体相同。
   */
  join(inviteCode: string): Promise<CourseSummaryDto> {
    return http.post<CourseSummaryDto>("/courses/join", { invite_code: inviteCode });
  },

  /** 契约 3：GET /courses/{course_id}/members —— 仅创建教师 */
  members(courseId: string, page = 1, pageSize = 20): Promise<Page<CourseMemberSummaryDto>> {
    return http.get<Page<CourseMemberSummaryDto>>(
      `/courses/${courseId}/members${buildQuery({ page, page_size: pageSize })}`,
    );
  },
};
