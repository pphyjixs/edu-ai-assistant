/**
 * 全局侧栏（首页等非课程页面使用）。
 *
 * 低噪声导航 + 最近课程 + 最近对话 + 用户区（DEVELOPMENT_SPEC 第 2.2 节 A）。
 * 「最近对话」只合并契约 6 已有的按课程会话列表，没有新增聚合接口。
 */

import { Link, NavLink, useNavigate } from "react-router-dom";

import { Icon, type IconName } from "@/components/Icon/Icon";
import { Skeleton } from "@/components/Skeleton/Skeleton";
import { useCurrentUser } from "@/features/auth/hooks/useCurrentUser";
import { useBuddyStore } from "@/features/buddy/store/buddyStore";
import { useRecentChatSessions } from "@/features/buddy/hooks/useBuddyThread";
import { useCourses } from "@/features/courses/hooks/useCourses";

import styles from "./GlobalSidebar.module.css";

const NAV_ITEMS: Array<{ to: string; label: string; icon: IconName; end?: boolean }> = [
  { to: "/", label: "首页", icon: "home", end: true },
  { to: "/courses", label: "我的课程", icon: "courses" },
  { to: "/tasks", label: "任务", icon: "tasks" },
  { to: "/workspace", label: "学习空间", icon: "workspace" },
];

export function GlobalSidebar() {
  const navigate = useNavigate();
  const userQuery = useCurrentUser();
  const coursesQuery = useCourses();

  const courses = coursesQuery.data ?? [];
  const recentCourses = courses.slice(0, 3);
  const { sessions, isPending: sessionsPending } = useRecentChatSessions(
    courses.map((course) => course.id),
    3,
  );

  const openBuddy = useBuddyStore((state) => state.openBuddy);
  const clearChatSession = useBuddyStore((state) => state.clearChatSession);
  const openChatSession = useBuddyStore((state) => state.openChatSession);

  function startNewConversation() {
    clearChatSession();
    openBuddy();
    navigate("/");
  }

  return (
    <aside className={styles.sidebar}>
      <div className={styles.brand}>
        <img
          className={styles.brandMark}
          src="/svg/icons/logo-studybuddy.svg"
          alt=""
          width={32}
          height={32}
          aria-hidden="true"
        />
        <div className={styles.brandText}>
          <span className={styles.brandName}>StudyBuddy</span>
          <span className={styles.brandSub}>教学 Agent 工作台</span>
        </div>
      </div>

      <button type="button" className={styles.newChat} onClick={startNewConversation}>
        <Icon name="plus" size={16} />
        <span>新建对话</span>
      </button>

      <nav className={styles.nav} aria-label="主导航">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) =>
              isActive ? `${styles.navItem} ${styles.navItemActive}` : styles.navItem
            }
          >
            <Icon name={item.icon} size={18} />
            <span>{item.label}</span>
          </NavLink>
        ))}
      </nav>

      <section className={styles.section} aria-label="最近课程">
        <p className={styles.sectionLabel}>最近课程</p>
        {coursesQuery.isPending ? (
          <div className={styles.skeletons}>
            <Skeleton height={30} radius="10px" />
            <Skeleton height={30} radius="10px" />
          </div>
        ) : (
          recentCourses.map((course) => (
            <Link
              key={course.id}
              to={`/courses/${course.id}`}
              className={styles.courseLink}
              title={course.name}
            >
              <span className={`${styles.dot} ${styles[course.accent]}`} />
              <span className={styles.courseLinkText}>{course.name}</span>
            </Link>
          ))
        )}
      </section>

      <section className={styles.section} aria-label="最近对话">
        <p className={styles.sectionLabel}>最近对话</p>
        {sessionsPending ? (
          <div className={styles.skeletons}>
            <Skeleton height={28} radius="10px" />
          </div>
        ) : sessions.length === 0 ? (
          <p className={styles.sectionEmpty}>还没有对话，试着问 Buddy 一个问题。</p>
        ) : (
          sessions.map((session) => (
            <button
              key={session.id}
              type="button"
              className={styles.courseLink}
              title={session.title}
              onClick={() => {
                openChatSession(session.id, session.courseId);
                openBuddy();
                navigate(`/courses/${session.courseId}`);
              }}
            >
              <Icon name="spark" size={12} />
              <span className={styles.courseLinkText}>{session.title}</span>
            </button>
          ))
        )}
      </section>

      <div className={styles.footer}>
        <span className={styles.avatar} aria-hidden="true">
          {userQuery.data?.initial ?? "·"}
        </span>
        <div className={styles.userMeta}>
          <strong>{userQuery.data?.displayName ?? "加载中"}</strong>
          <span>{userQuery.data?.roleLabel ?? ""}</span>
        </div>
      </div>
    </aside>
  );
}
