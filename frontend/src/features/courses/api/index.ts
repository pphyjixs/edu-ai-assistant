/**
 * Courses 的 API 入口。
 *
 * 后端实现后换成基于 services/http 的实现：
 *   http.get<CourseDto[]>("/courses")
 *   http.get<CourseDto>(`/courses/${courseId}`)
 */

import type { CoursesApi } from "./contracts";
import { mockCoursesApi } from "./mock";

export const coursesApi: CoursesApi = mockCoursesApi;

export { MOCK_COURSE_IDS } from "./mock";
export type { CourseDto, CourseMemberDto, CourseStatusDto, CoursesApi } from "./contracts";
