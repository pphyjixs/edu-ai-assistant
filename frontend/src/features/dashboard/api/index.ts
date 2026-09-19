/**
 * Dashboard 的 API 入口。
 *
 * 后端实现后换成基于 services/http 的实现：
 *   http.get<DashboardStudentDto>("/dashboard/student")
 *   http.get<DashboardTeacherDto>("/dashboard/teacher")
 */

import type { DashboardApi } from "./contracts";
import { mockDashboardApi } from "./mock";

export const dashboardApi: DashboardApi = mockDashboardApi;

export type {
  DashboardApi,
  DashboardStudentDto,
  DashboardTaskDto,
  DashboardTeacherDto,
} from "./contracts";
