/**
 * Courses 的 feature 级 mock adapter。
 *
 * 课程 id 是演示种子数据，assignments / dashboard 的 mock 使用同一组 id，
 * 改名时三处要一起改（真实接口接入后本文件整体删除）。
 */

import { HttpError } from "@/services/http";

import type { CoursesApi, CourseDto } from "./contracts";

const DELAY_MS = 320;

/** 演示课程 id（供其他 feature 的 mock 对齐） */
export const MOCK_COURSE_IDS = {
  ai: "b1a7c3e2-4d58-4f90-9c21-6e7a8b0d1f34",
  database: "c2b8d4f3-5e69-4a01-8d32-7f8b9c1e2a45",
  system: "d3c9e504-6f7a-4b12-9e43-8a9c0d2f3b56",
} as const;

const COURSES: CourseDto[] = [
  {
    id: MOCK_COURSE_IDS.ai,
    name: "人工智能课程实训",
    description: "理解 Self-Attention 与 Transformer，并完成一个简化版文本分类实验。",
    teacher: { id: "t-0001", display_name: "王老师" },
    status: "ACTIVE",
    invite_code: "AI2026",
    created_at: "2026-08-28T01:00:00Z",
  },
  {
    id: MOCK_COURSE_IDS.database,
    name: "数据库系统",
    description: "关系模型、SQL 查询与事务处理，配套多表查询与索引实验。",
    teacher: { id: "t-0002", display_name: "李老师" },
    status: "ACTIVE",
    invite_code: "DB2026",
    created_at: "2026-08-29T01:00:00Z",
  },
  {
    id: MOCK_COURSE_IDS.system,
    name: "计算机系统",
    description: "从数据表示到存储层次，包含浮点运算与流水线相关实验。",
    teacher: { id: "t-0003", display_name: "陈老师" },
    status: "ACTIVE",
    invite_code: "CS2026",
    created_at: "2026-08-30T01:00:00Z",
  },
];

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

export const mockCoursesApi: CoursesApi = {
  async listMyCourses(): Promise<CourseDto[]> {
    await delay(DELAY_MS);
    return COURSES;
  },

  async getCourse(courseId: string): Promise<CourseDto> {
    await delay(DELAY_MS);
    const found = COURSES.find((course) => course.id === courseId);
    if (!found) {
      throw new HttpError({
        code: "RESOURCE_NOT_FOUND",
        message: "课程不存在，或者你还没有加入这门课程。",
        status: 404,
      });
    }
    return found;
  },
};
