/**
 * 路由表。
 *
 * 结构上对齐 DEVELOPMENT_SPEC 第 5 节：仪表盘与课程 Workspace 是两条
 * 并列的布局路由，课程内的所有页面共用同一个 CourseWorkspaceLayout。
 *
 * 已接真实后端的模块（课程、资料、问答、练习）用真实页面；
 * 后端尚未实现的模块（作业、成绩、AI 批改）落到统一的占位页，
 * 明确标出「属于哪个模块、下一阶段实现」，不留点进去就白屏的死链。
 */

import type { ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { AuthGuard } from "./guards/AuthGuard";
import { RoleGuard } from "./guards/RoleGuard";
import { CourseWorkspaceLayout } from "./layouts/CourseWorkspaceLayout";
import { DashboardLayout } from "./layouts/DashboardLayout";
import { NotFoundPage } from "./pages/NotFoundPage";
import { PlaceholderPage } from "./pages/PlaceholderPage";
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
        <Route
          path="/tasks"
          element={
            <PlaceholderPage
              title="任务"
              description="作业与提交相关的任务列表依赖 assignments 模块，后端尚未实现，下一阶段补齐。首页「今日待办」目前使用示例数据。"
            />
          }
        />
        <Route
          path="/workspace"
          element={
            <PlaceholderPage
              title="学习空间"
              description="个人学习空间需要跨课程的答题记录聚合接口，当前契约没有提供，下一阶段与后端确认后再实现。"
            />
          }
        />
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
