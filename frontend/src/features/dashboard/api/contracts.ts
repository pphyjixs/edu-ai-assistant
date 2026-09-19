/**
 * Dashboard 的 DTO 定义（docs/api-contract.md 第 11 节）。
 *
 * 契约明确：Dashboard 只返回页面首屏需要的摘要和最近记录，
 * 不返回完整业务列表。因此这里只有计数与「最近几条」。
 *
 * 课程列表不在这里重复返回——首页的课程卡片直接复用 courses 查询，
 * 避免同一份数据在两处各有一份真相。
 */

import type { AssignmentStatusDto } from "@/features/assignments/api";

export type DashboardTaskDto = {
  id: string;
  /** 点击后进入作业详情 */
  assignment_id: string;
  title: string;
  course_id: string;
  course_name: string;
  due_at: string | null;
  /** 沿用契约 8 的作业状态枚举，dashboard 不另造一套状态 */
  assignment_status: AssignmentStatusDto;
  /** 本人是否已提交；由 grading 模块聚合，dashboard 只负责读取 */
  submitted: boolean;
};

export type DashboardStudentDto = {
  /** 待完成任务总数，首页只展示最近若干条 */
  pending_task_count: number;
  tasks: DashboardTaskDto[];
};

export type DashboardTeacherDto = {
  course_count: number;
  pending_review_count: number;
  /** 最近提交 */
  tasks: DashboardTaskDto[];
};

export interface DashboardApi {
  /** 契约 11：GET /dashboard/student */
  getStudentDashboard(): Promise<DashboardStudentDto>;
  /** 契约 11：GET /dashboard/teacher */
  getTeacherDashboard(): Promise<DashboardTeacherDto>;
}
