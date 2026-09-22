/**
 * 首页 Dashboard。
 *
 * 回答四件事：今天要做什么、我有哪些课程、最近学到哪里、Buddy 现在能帮什么
 * （DEVELOPMENT_SPEC 第 7.1 节）。页面只负责组织区块与声明 Buddy 上下文，
 * 数据来自 dashboard / courses 两个 feature 的查询。
 */

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { ErrorState } from "@/components/ErrorState/ErrorState";
import { useCurrentUser } from "@/features/auth/hooks/useCurrentUser";
import { useAskBuddy, useSetBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { useCourses } from "@/features/courses/hooks/useCourses";
import { CourseGrid } from "@/features/courses/components/CourseGrid/CourseGrid";
import { BuddyOmnibox } from "@/features/dashboard/components/BuddyOmnibox/BuddyOmnibox";
import { GreetingHero } from "@/features/dashboard/components/GreetingHero/GreetingHero";
import { TaskGrid } from "@/features/dashboard/components/TaskGrid/TaskGrid";
import { useDashboard } from "@/features/dashboard/hooks/useDashboard";
import { toAppError } from "@/services/http";

import styles from "./DashboardPage.module.css";

export function DashboardPage() {
  const userQuery = useCurrentUser();
  const role = userQuery.data?.role ?? "student";

  const dashboardQuery = useDashboard(role);
  const coursesQuery = useCourses();
  const askBuddy = useAskBuddy();

  const courses = coursesQuery.data ?? [];
  const [selectedCourseId, setSelectedCourseId] = useState<string | undefined>(undefined);

  // 默认选中最近一门课程：契约 6 的会话按课程创建，必须先有课程才能提问
  useEffect(() => {
    if (!selectedCourseId && courses.length > 0) {
      setSelectedCourseId(courses[0].id);
    }
  }, [selectedCourseId, courses]);

  // 首页只需要声明「当前在跟哪门课对话」，不需要组装任何请求
  useSetBuddyContext({ courseId: selectedCourseId, route: "" });

  const summary = dashboardQuery.summary;

  // role 同时决定数据形状，因此用 hook 返回的 role 做窄化，而不是页面上的 role
  const heading =
    dashboardQuery.role === "teacher"
      ? {
          title: "需要我处理的",
          description: `我创建的进行中课程 ${dashboardQuery.summary.activeCourseCount} 门 · 待发布任务 ${dashboardQuery.summary.draftCount} 个`,
        }
      : {
          title: "待办任务",
          description: `共 ${dashboardQuery.summary.pendingCount} 项可提交${
            dashboardQuery.summary.closedCount > 0
              ? `，${dashboardQuery.summary.closedCount} 项已关闭`
              : ""
          }`,
        };

  return (
    <div className={styles.page}>
      <GreetingHero displayName={userQuery.data?.displayName ?? "同学"}>
        <BuddyOmnibox
          courses={courses}
          isCoursesPending={coursesQuery.isPending}
          selectedCourseId={selectedCourseId}
          onSelectCourse={setSelectedCourseId}
          onAsk={(prompt) => askBuddy(prompt)}
        />
      </GreetingHero>

      <section className={styles.section}>
        <div className={styles.heading}>
          <div>
            <h2 className={styles.headingTitle}>{heading.title}</h2>
            {heading.description ? (
              <p className={styles.headingDescription}>{heading.description}</p>
            ) : null}
          </div>
          <Link to="/tasks" className={styles.textLink}>
            查看全部
          </Link>
        </div>

        {dashboardQuery.isError ? (
          <ErrorState
            message={toAppError(dashboardQuery.error).message}
            requestId={toAppError(dashboardQuery.error).requestId}
            onRetry={() => void dashboardQuery.refetch()}
          />
        ) : (
          <TaskGrid tasks={summary?.tasks ?? []} isPending={dashboardQuery.isPending} />
        )}
      </section>

      <section className={styles.section}>
        <div className={styles.heading}>
          <div>
            <h2 className={styles.headingTitle}>我的课程</h2>
            <p className={styles.headingDescription}>进入课程工作区，资料、作业和 Buddy 都在那里</p>
          </div>
        </div>

        {coursesQuery.isError ? (
          <ErrorState
            message={toAppError(coursesQuery.error).message}
            requestId={toAppError(coursesQuery.error).requestId}
            onRetry={() => void coursesQuery.refetch()}
          />
        ) : (
          <CourseGrid courses={courses} isPending={coursesQuery.isPending} />
        )}
      </section>
    </div>
  );
}
