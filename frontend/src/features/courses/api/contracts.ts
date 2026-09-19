/**
 * Courses 的 DTO 定义。
 *
 * 后端 courses 模块尚未实现（backend/app/modules/courses 只有占位文件），
 * 端点按 docs/api-contract.md 第 3 节，字段按 docs/architecture.md 第 6 节
 * 的核心对象表（Course：名称、描述、教师、邀请码、状态）取最小集合。
 *
 * 刻意不包含人数、学期、学习进度——契约与核心对象都没有这些字段，
 * 前端不得为了画面好看自行补造（DEVELOPMENT_SPEC 第 7.4 节）。
 */

import type { UserRoleDto } from "@/features/auth/api";

export type CourseStatusDto = "ACTIVE" | "ARCHIVED";

export type CourseTeacherDto = {
  id: string;
  display_name: string;
};

export type CourseDto = {
  id: string;
  name: string;
  description: string;
  teacher: CourseTeacherDto;
  status: CourseStatusDto;
  /** 仅课程教师自己可见；学生拿到的值为 null */
  invite_code: string | null;
  created_at: string;
};

export type CourseMemberDto = {
  id: string;
  user_id: string;
  display_name: string;
  role: UserRoleDto;
  joined_at: string;
};

export interface CoursesApi {
  /** 契约 3：GET /courses —— 我参加的课程 */
  listMyCourses(): Promise<CourseDto[]>;
  /** 契约 3：GET /courses/{course_id} */
  getCourse(courseId: string): Promise<CourseDto>;
}
