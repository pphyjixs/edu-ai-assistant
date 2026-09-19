/**
 * 路由表。
 *
 * 结构上对齐 DEVELOPMENT_SPEC 第 5 节：仪表盘与课程 Workspace 是两条
 * 并列的布局路由，课程内的所有页面共用同一个 CourseWorkspaceLayout。
 *
 * 本阶段只实现首页与作业详情；其余路由先落到统一的占位页，
 * 明确标出「尚未实现」而不是留一条点进去就白屏的死链。
 */

import { Route, Routes } from "react-router-dom";

import { AssignmentDetailPage } from "@/features/assignments/pages/AssignmentDetailPage";
import { DashboardPage } from "@/features/dashboard/pages/DashboardPage";

import { CourseWorkspaceLayout } from "./layouts/CourseWorkspaceLayout";
import { DashboardLayout } from "./layouts/DashboardLayout";
import { NotFoundPage } from "./pages/NotFoundPage";
import { PlaceholderPage } from "./pages/PlaceholderPage";

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<DashboardLayout />}>
        <Route path="/" element={<DashboardPage />} />
        <Route
          path="/courses"
          element={
            <PlaceholderPage
              title="我的课程"
              description="课程列表与加入课程将在下一阶段实现。首页的课程卡片已经可以进入课程工作区。"
            />
          }
        />
        <Route
          path="/tasks"
          element={
            <PlaceholderPage
              title="任务"
              description="全局任务列表将在下一阶段实现。首页「今日待办」已经展示最近的任务。"
            />
          }
        />
        <Route
          path="/workspace"
          element={
            <PlaceholderPage
              title="学习空间"
              description="个人学习空间将在下一阶段实现。"
            />
          }
        />
      </Route>

      <Route path="/courses/:courseId" element={<CourseWorkspaceLayout />}>
        <Route
          index
          element={
            <PlaceholderPage
              title="课程概览"
              description="课程概览页将在下一阶段实现。可以先从「作业」进入作业详情。"
            />
          }
        />
        <Route
          path="materials"
          element={
            <PlaceholderPage
              title="课程资料"
              description="资料列表、上传与解析状态属于 materials 模块，将在下一阶段实现。"
            />
          }
        />
        <Route
          path="materials/:materialId"
          element={
            <PlaceholderPage
              title="资料阅读"
              description="资料阅读器属于 materials 模块，将在下一阶段实现。"
            />
          }
        />
        <Route
          path="learn"
          element={
            <PlaceholderPage
              title="学习"
              description="章节学习与 AI 练习属于 learning 模块，将在下一阶段实现。"
            />
          }
        />
        <Route
          path="learn/:sectionId"
          element={
            <PlaceholderPage
              title="章节学习"
              description="章节学习属于 learning 模块，将在下一阶段实现。"
            />
          }
        />
        <Route
          path="assignments"
          element={
            <PlaceholderPage
              title="作业列表"
              description="作业列表将在下一阶段实现。首页的「今日待办」可以直接进入作业详情。"
            />
          }
        />
        <Route path="assignments/:assignmentId" element={<AssignmentDetailPage />} />
        <Route
          path="grades"
          element={
            <PlaceholderPage
              title="成绩与反馈"
              description="成绩与反馈属于 grading 模块，将在下一阶段实现。"
            />
          }
        />
        <Route
          path="manage"
          element={
            <PlaceholderPage
              title="课程管理"
              description="课程管理属于 courses 模块的教师端功能，将在下一阶段实现。"
            />
          }
        />
        <Route
          path="manage/members"
          element={
            <PlaceholderPage
              title="学生与邀请码"
              description="成员列表与邀请码管理将在下一阶段实现。"
            />
          }
        />
        <Route
          path="manage/materials"
          element={
            <PlaceholderPage
              title="资料管理"
              description="教师端资料上传与管理将在下一阶段实现。"
            />
          }
        />
        <Route
          path="manage/assignments"
          element={
            <PlaceholderPage
              title="任务管理"
              description="实验任务的创建、评分项配置与发布将在下一阶段实现。"
            />
          }
        />
        <Route
          path="grading"
          element={
            <PlaceholderPage
              title="AI 批改"
              description="提交列表、AI 分项建议与教师复核属于 grading 模块，将在下一阶段实现。"
            />
          }
        />
        <Route
          path="grading/:assignmentId"
          element={
            <PlaceholderPage
              title="AI 批改"
              description="提交列表、AI 分项建议与教师复核属于 grading 模块，将在下一阶段实现。"
            />
          }
        />
      </Route>

      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
