/**
 * Dashboard 的 feature 级 mock adapter。
 *
 * 任务里引用的 assignment_id 与 courses / assignments 的 mock 种子数据一致，
 * 保证首页卡片点进去能打开真实的作业详情页。
 */

import type { DashboardApi, DashboardStudentDto, DashboardTaskDto, DashboardTeacherDto } from "./contracts";

const DELAY_MS = 380;
const DAY_MS = 24 * 60 * 60 * 1000;

const COURSE_AI = "b1a7c3e2-4d58-4f90-9c21-6e7a8b0d1f34";
const COURSE_DATABASE = "c2b8d4f3-5e69-4a01-8d32-7f8b9c1e2a45";

function isoFromNow(deltaMs: number): string {
  return new Date(Date.now() + deltaMs).toISOString();
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

const STUDENT_TASKS: DashboardTaskDto[] = [
  {
    id: "task-0001",
    assignment_id: "a7f3d1b2-9c4e-4a6f-8b1d-2e3f4a5b6c70",
    title: "实验二：Transformer 文本分类实验",
    course_id: COURSE_AI,
    course_name: "人工智能课程实训",
    due_at: isoFromNow(DAY_MS),
    assignment_status: "PUBLISHED",
    submitted: false,
  },
  {
    id: "task-0002",
    assignment_id: "d1a6e4f5-2f71-4d92-9e40-5b6c7d8e9fa3",
    title: "SQL 多表查询练习",
    course_id: COURSE_DATABASE,
    course_name: "数据库系统",
    due_at: isoFromNow(3 * DAY_MS),
    assignment_status: "PUBLISHED",
    submitted: false,
  },
  {
    id: "task-0003",
    assignment_id: "c9f5d3e4-1e60-4c81-8d3f-4a5b6c7d8e92",
    title: "实验三：注意力可视化与误差分析",
    course_id: COURSE_AI,
    course_name: "人工智能课程实训",
    due_at: isoFromNow(8 * DAY_MS),
    assignment_status: "PUBLISHED",
    submitted: false,
  },
  {
    id: "task-0004",
    assignment_id: "b8e4c2d3-0d5f-4b70-9c2e-3f4a5b6c7d81",
    title: "实验一：需求分析与用例建模",
    course_id: COURSE_AI,
    course_name: "人工智能课程实训",
    due_at: isoFromNow(-6 * DAY_MS),
    assignment_status: "CLOSED",
    submitted: true,
  },
];

export const mockDashboardApi: DashboardApi = {
  async getStudentDashboard(): Promise<DashboardStudentDto> {
    await delay(DELAY_MS);
    return {
      pending_task_count: STUDENT_TASKS.filter((task) => !task.submitted).length,
      // 契约 11：只返回最近记录，其余进 /tasks
      tasks: STUDENT_TASKS.slice(0, 4),
    };
  },

  async getTeacherDashboard(): Promise<DashboardTeacherDto> {
    await delay(DELAY_MS);
    return {
      course_count: 2,
      pending_review_count: 3,
      tasks: STUDENT_TASKS.filter((task) => task.submitted).concat({
        id: "task-9001",
        assignment_id: "a7f3d1b2-9c4e-4a6f-8b1d-2e3f4a5b6c70",
        title: "实验二：Transformer 文本分类实验",
        course_id: COURSE_AI,
        course_name: "人工智能课程实训",
        due_at: isoFromNow(DAY_MS),
        assignment_status: "PUBLISHED",
        submitted: true,
      }),
    };
  },
};
