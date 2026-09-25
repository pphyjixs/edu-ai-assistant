import { Link } from "react-router-dom";

import { Skeleton } from "@/components/Skeleton/Skeleton";
import { useCurrentUser } from "@/features/auth/hooks/useCurrentUser";
import { useCourses } from "@/features/courses/hooks/useCourses";
import { useDashboard } from "@/features/dashboard/hooks/useDashboard";

import styles from "./DashboardRightRail.module.css";

type DashboardRightRailProps = {
  activeCourseId?: string;
};

export function DashboardRightRail({ activeCourseId }: DashboardRightRailProps) {
  const userQuery = useCurrentUser();
  const role = userQuery.data?.role ?? "student";
  const dashboardQuery = useDashboard(role);
  const coursesQuery = useCourses();
  const courses = coursesQuery.data ?? [];
  const tasks = dashboardQuery.summary.tasks;

  const taskSummary =
    dashboardQuery.role === "teacher"
      ? `进行中课程 ${dashboardQuery.summary.activeCourseCount} 门 · 待发布 ${dashboardQuery.summary.draftCount} 项`
      : `可提交 ${dashboardQuery.summary.pendingCount} 项${
          dashboardQuery.summary.closedCount > 0
            ? ` · 已关闭 ${dashboardQuery.summary.closedCount} 项`
            : ""
        }`;

  return (
    <aside className={styles.rail} aria-label="课程与任务概览">
      <div className={styles.railHeader}>
        <span className={styles.eyebrow}>学习工作台</span>
        <h2>课程概览</h2>
        <p>在对话旁查看课程和近期任务。</p>
      </div>

      <section className={styles.section}>
        <div className={styles.sectionHeading}>
          <div>
            <h3>{role === "teacher" ? "需要我处理的" : "待办任务"}</h3>
            <p>{taskSummary}</p>
          </div>
          <Link to="/tasks">全部</Link>
        </div>

        {dashboardQuery.isPending ? (
          <div className={styles.skeletonList}>
            <Skeleton height={68} radius="12px" />
            <Skeleton height={68} radius="12px" />
          </div>
        ) : dashboardQuery.isError ? (
          <div className={styles.inlineState}>
            <span>任务暂时加载失败</span>
            <button type="button" onClick={() => void dashboardQuery.refetch()}>
              重试
            </button>
          </div>
        ) : tasks.length === 0 ? (
          <p className={styles.empty}>暂时没有需要处理的任务</p>
        ) : (
          <div className={styles.list}>
            {tasks.map((task) => (
              <Link key={task.id} to={task.href} className={styles.taskItem}>
                <span className={styles.itemTopline}>
                  <strong>{task.title}</strong>
                  <em>{task.badgeLabel}</em>
                </span>
                <span className={styles.itemMeta}>
                  {task.courseName} · {task.dueLabel}
                </span>
              </Link>
            ))}
          </div>
        )}
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeading}>
          <div>
            <h3>我的课程</h3>
            <p>共 {courses.length} 门课程</p>
          </div>
          <Link to="/courses">全部</Link>
        </div>

        {coursesQuery.isPending ? (
          <div className={styles.skeletonList}>
            <Skeleton height={56} radius="12px" />
            <Skeleton height={56} radius="12px" />
          </div>
        ) : coursesQuery.isError ? (
          <div className={styles.inlineState}>
            <span>课程暂时加载失败</span>
            <button type="button" onClick={() => void coursesQuery.refetch()}>
              重试
            </button>
          </div>
        ) : courses.length === 0 ? (
          <p className={styles.empty}>还没有加入课程</p>
        ) : (
          <div className={styles.list}>
            {courses.slice(0, 6).map((course) => (
              <Link
                key={course.id}
                to={`/courses/${course.id}`}
                className={`${styles.courseItem} ${
                  activeCourseId === course.id ? styles.courseItemActive : ""
                }`}
              >
                <span className={`${styles.courseDot} ${styles[course.accent]}`} />
                <span className={styles.courseText}>
                  <strong>{course.name}</strong>
                  <span>{course.statusLabel} · 更新于 {course.updatedAtLabel}</span>
                </span>
              </Link>
            ))}
          </div>
        )}
      </section>
    </aside>
  );
}
