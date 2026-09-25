/**
 * 路由表。
 *
 * 结构上对齐 DEVELOPMENT_SPEC 第 5 节：仪表盘与课程 Workspace 是两条
 * 并列的布局路由，课程内的所有页面共用同一个 CourseWorkspaceLayout。
 *
 * 所有入口都指向真实页面：课程、资料、问答、练习、实验任务、提交与批改、
 * 跨课程任务（``/tasks``）。仪表盘区域的「学习空间」已移除——它依赖的
 * 跨课程答题记录聚合接口不存在，与其留一个点进去白屏的占位页，不如砍掉入口。
 *
 * ``pages/PlaceholderPage.tsx`` 仍保留给将来的新模块使用，
 * 但**不要**为已有接口的模块放占位页。
 */

import type { ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { AuthGuard } from "./guards/AuthGuard";
import { RoleGuard } from "./guards/RoleGuard";
import { CourseWorkspaceLayout } from "./layouts/CourseWorkspaceLayout";
import { DashboardLayout } from "./layouts/DashboardLayout";
import { NotFoundPage } from "./pages/NotFoundPage";
import { AssignmentDetailPage } from "@/features/assignments/pages/AssignmentDetailPage";
import { AssignmentsPage } from "@/features/assignments/pages/AssignmentsPage";
import { LoginPage } from "@/features/auth/pages/LoginPage";
import { CourseManagePage } from "@/features/courses/pages/CourseManagePage";
import { CourseOverviewPage } from "@/features/courses/pages/CourseOverviewPage";
import { CoursesPage } from "@/features/courses/pages/CoursesPage";
import { DashboardPage } from "@/features/dashboard/pages/DashboardPage";
import { HomeChatPage } from "@/features/buddy/pages/HomeChatPage";
import { GradesPage } from "@/features/grading/pages/GradesPage";
import { GradingWorkbenchPage } from "@/features/grading/pages/GradingWorkbenchPage";
import { SubmissionDetailPage } from "@/features/grading/pages/SubmissionDetailPage";
import { SubmissionsPage } from "@/features/grading/pages/SubmissionsPage";
import { MaterialReaderPage } from "@/features/materials/pages/MaterialReaderPage";
import { MaterialsPage } from "@/features/materials/pages/MaterialsPage";
import { LearnPage } from "@/features/practice/pages/LearnPage";
import { PracticePage } from "@/features/practice/pages/PracticePage";
import { TasksPage } from "@/features/tasks/pages/TasksPage";
import { hasStoredTokens } from "@/features/auth/hooks/useCurrentUser";

/** 登录页：已登录时直接回到目标页，避免出现「登录后又看到登录页」 */
function PublicOnly({ children }: { children: ReactNode }) {
  const location = useLocation();
  if (hasStoredTokens()) {
    const from = (location.state as { from?: string } | null)?.from;
    return <Navigate to={from ?? "/"} replace />;
  }
  return <>{children}</>;
}

/** 受保护区域：先要求登录，再按需校验角色 */
function Protected({ children, allow }: { children: ReactNode; allow?: Array<"teacher" | "student"> }) {
  return (
    <AuthGuard>
      {allow ? <RoleGuard allow={allow}>{children}</RoleGuard> : children}
    </AuthGuard>
  );
}

export function AppRoutes() {
  return (
    <Routes>
      <Route
        path="/login"
        element={
          <PublicOnly>
            <LoginPage />
          </PublicOnly>
        }
      />

      {/* ------------------------- 仪表盘区域 ------------------------- */}
      <Route
        element={
          <Protected>
            <DashboardLayout />
          </Protected>
        }
      >
        <Route path="/" element={<DashboardPage />} />
        {/* 首页中央会话：消息在中央显示，不弹出右侧抽屉（开发方案 4.1） */}
        <Route path="/chats/:sessionId" element={<HomeChatPage />} />
        <Route path="/courses" element={<CoursesPage />} />
        {/* 任务：跨课程待办列表，与首页「今日待办」共用同一份数据源 */}
        <Route path="/tasks" element={<TasksPage />} />
      </Route>

      {/* ------------------------ 课程 WorkSpace ------------------------ */}
      <Route
        path="/courses/:courseId"
        element={
          <Protected>
            <CourseWorkspaceLayout />
          </Protected>
        }
      >
        <Route index element={<CourseOverviewPage />} />

        <Route path="materials" element={<MaterialsPage />} />
        <Route path="materials/:materialId" element={<MaterialReaderPage />} />

        <Route path="learn" element={<LearnPage />} />
        <Route path="learn/:setId" element={<PracticePage />} />

        <Route
          path="manage"
          element={
            <Protected allow={["teacher"]}>
              <CourseManagePage />
            </Protected>
          }
        />

        {/* 实验任务已接入真实接口（契约第 8 节） */}
        <Route path="assignments" element={<AssignmentsPage />} />
        <Route path="assignments/:assignmentId" element={<AssignmentDetailPage />} />
        <Route
          path="assignments/:assignmentId/submissions"
          element={
            <Protected allow={["teacher"]}>
              <SubmissionsPage />
            </Protected>
          }
        />

        {/* 提交与批改（契约第 9 节）：学生看成绩，教师批改与发布 */}
        <Route path="submissions/:submissionId" element={<SubmissionDetailPage />} />
        <Route path="grades" element={<GradesPage />} />
        <Route
          path="grading"
          element={
            <Protected allow={["teacher"]}>
              <GradingWorkbenchPage />
            </Protected>
          }
        />
      </Route>

      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
