/**
 * 课程 Workspace 布局：课程侧栏 + 当前学习对象 + Buddy。
 *
 * 三栏比例来自 DEVELOPMENT_SPEC 第 2.2 节 B：224 / fluid / 360。
 * 侧栏收起与 Buddy 折叠都通过 CSS 变量驱动列宽，这样媒体查询
 * 仍能在窄屏下把 Buddy 改成浮层（内联样式只影响变量，不锁死列定义）。
 */

import type { CSSProperties } from "react";
import { Outlet, useLocation, useParams } from "react-router-dom";

import { Button } from "@/components/Button/Button";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { BuddyFab } from "@/features/buddy/components/BuddyFab/BuddyFab";
import { BuddyPanel } from "@/features/buddy/components/BuddyPanel/BuddyPanel";
import { useBuddyPanelControls, useBuddyOpen } from "@/features/buddy/hooks/useBuddy";
import { useBuddyStore } from "@/features/buddy/store/buddyStore";
import { useCourse } from "@/features/courses/hooks/useCourses";
import { toAppError } from "@/services/http";

import { CourseSidebar } from "./CourseSidebar";
import { TopBar } from "./TopBar";

import styles from "./CourseWorkspaceLayout.module.css";

/** 面包屑的二级名称。作业标题由页面自己展示，这里只到板块层级。 */
function sectionLabelFor(pathname: string, courseId: string | undefined): string {
  if (!courseId) return "";
  const rest = pathname.slice(`/courses/${courseId}`.length).replace(/^\//, "");
  const [first = ""] = rest.split("/");

  switch (first) {
    case "":
      return "概览";
    case "materials":
      return "课程资料";
    case "learn":
      return "学习";
    case "assignments":
      return "作业";
    case "grades":
      return "成绩";
    case "grading":
      return "AI 批改";
    case "manage":
      return "课程管理";
    default:
      return "";
  }
}

export function CourseWorkspaceLayout() {
  const { courseId } = useParams<{ courseId: string }>();
  const location = useLocation();

  const courseQuery = useCourse(courseId);
  const buddyOpen = useBuddyOpen();
  const { toggleBuddy } = useBuddyPanelControls();
  const sidebarCollapsed = useBuddyStore((state) => state.courseSidebarCollapsed);
  const toggleCourseSidebar = useBuddyStore((state) => state.toggleCourseSidebar);

  const style = {
    "--sidebar-current": sidebarCollapsed ? "0px" : "var(--sidebar-course-width)",
    "--buddy-column": buddyOpen ? "var(--buddy-panel-width)" : "0px",
  } as CSSProperties;

  const sectionLabel = sectionLabelFor(location.pathname, courseId);
  const courseError = courseQuery.isError ? toAppError(courseQuery.error) : null;

  return (
    <div className={styles.workspace} style={style}>
      {/*
        侧栏收起时仍然渲染占位的槽位，只把列宽压到 0 并裁掉内容。
        如果直接卸载整棵侧栏，网格的自动放置会把主内容排进那个 0px 的列里，
        整页会被压扁——所以这里不能靠「不渲染」来收起。
      */}
      <div
        className={sidebarCollapsed ? styles.sidebarSlotCollapsed : styles.sidebarSlot}
        aria-hidden={sidebarCollapsed}
      >
        <CourseSidebar
          courseId={courseId ?? ""}
          course={courseQuery.data}
          isLoading={courseQuery.isPending}
        />
      </div>

      <div className={styles.main}>
        <TopBar
          divided
          left={
            <div className={styles.breadcrumb}>
              <Button
                variant="ghost"
                size="sm"
                iconLeft="courses"
                onClick={toggleCourseSidebar}
                aria-label={sidebarCollapsed ? "展开课程导航" : "收起课程导航"}
              />
              <span className={styles.crumbMuted}>{courseQuery.data?.name ?? "课程"}</span>
              {sectionLabel ? (
                <>
                  <span className={styles.crumbSeparator} aria-hidden="true">
                    ›
                  </span>
                  <strong className={styles.crumbStrong}>{sectionLabel}</strong>
                </>
              ) : null}
            </div>
          }
          right={
            <Button
              variant="ghost"
              size="sm"
              iconLeft="spark"
              onClick={toggleBuddy}
              aria-expanded={buddyOpen}
            >
              {buddyOpen ? "收起 Buddy" : "Buddy"}
            </Button>
          }
        />

        <div className={styles.content}>
          {courseError ? (
            <ErrorState
              title="课程加载失败"
              message={courseError.message}
              requestId={courseError.requestId}
              onRetry={() => void courseQuery.refetch()}
            />
          ) : (
            <Outlet />
          )}
        </div>
      </div>

      <BuddyPanel variant="docked" />
      <BuddyFab />
    </div>
  );
}
