/**
 * 首页数据 Hook。按角色取不同的 dashboard 摘要（契约 11）。
 *
 * 两种角色返回的结构不同，这里用一个带判别字段的载荷包住，
 * 让 select 里的窄化对 TypeScript 可见，而不是靠类型断言糊过去。
 */

import { useQuery } from "@tanstack/react-query";

import { queryKeys } from "@/services/queryKeys";

import { dashboardApi, type DashboardStudentDto, type DashboardTeacherDto } from "../api";
import { toTodayTaskVM, type TodayTaskVM } from "../model/types";

type DashboardPayload =
  | { kind: "teacher"; data: DashboardTeacherDto }
  | { kind: "student"; data: DashboardStudentDto };

export type DashboardSummaryVM = {
  /** 学生：待完成任务数；教师：进行中课程数 */
  primaryCount: number;
  /** 教师特有的待批改数；学生为 null */
  reviewCount: number | null;
  tasks: TodayTaskVM[];
};

export function useDashboard(role: "teacher" | "student") {
  return useQuery<DashboardPayload, Error, DashboardSummaryVM>({
    queryKey: role === "teacher" ? queryKeys.dashboardTeacher : queryKeys.dashboardStudent,
    queryFn: async (): Promise<DashboardPayload> => {
      if (role === "teacher") {
        return { kind: "teacher", data: await dashboardApi.getTeacherDashboard() };
      }
      return { kind: "student", data: await dashboardApi.getStudentDashboard() };
    },
    select: (payload): DashboardSummaryVM => {
      if (payload.kind === "teacher") {
        return {
          primaryCount: payload.data.course_count,
          reviewCount: payload.data.pending_review_count,
          tasks: payload.data.tasks.map(toTodayTaskVM),
        };
      }
      return {
        primaryCount: payload.data.pending_task_count,
        reviewCount: null,
        tasks: payload.data.tasks.map(toTodayTaskVM),
      };
    },
  });
}
