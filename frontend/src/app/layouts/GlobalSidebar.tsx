/**
 * 全局侧栏（首页等非课程页面使用）。
 *
 * 低噪声导航 + 最近课程 + 最近对话 + 用户区（DEVELOPMENT_SPEC 第 2.2 节 A）。
 * 「最近对话」只合并契约 6 已有的按课程会话列表，没有新增聚合接口。
 */

import { useState } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";

import { Button } from "@/components/Button/Button";
import { Icon, type IconName } from "@/components/Icon/Icon";
import { Skeleton } from "@/components/Skeleton/Skeleton";
import { useLogout } from "@/features/auth/hooks/useAuthActions";
import { useCurrentUser } from "@/features/auth/hooks/useCurrentUser";
import { useBuddyStore } from "@/features/buddy/store/buddyStore";
import { useRecentChatSessions } from "@/features/buddy/hooks/useBuddyThread";
import { MySkillsDialog } from "@/features/buddy/components/MySkillsDialog/MySkillsDialog";
import { useCourses } from "@/features/courses/hooks/useCourses";

import styles from "./GlobalSidebar.module.css";

/**
 * 主导航。只保留有真实页面的入口：
 * 「学习空间」曾经是占位页，跨课程答题记录聚合接口不存在，已整体移除
 * （不留点进去就白屏的死链）。
 */
const NAV_ITEMS: Array<{ to: string; label: string; icon: IconName; end?: boolean }> = [
  { to: "/", label: "首页", icon: "home", end: true },
  { to: "/courses", label: "我的课程", icon: "courses" },
  { to: "/tasks", label: "任务", icon: "tasks" },
];

export function GlobalSidebar() {
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const navigate = useNavigate();
  const userQuery = useCurrentUser();
  const coursesQuery = useCourses();

  const courses = coursesQuery.data ?? [];
  const recentCourses = courses.slice(0, 3);

  const clearChatSession = useBuddyStore((state) => state.clearChatSession);
  const openChatSession = useBuddyStore((state) => state.openChatSession);
  const logout = useLogout();

  // 需要先拿到课程 id 才能按课程取会话（契约 6 的会话列表是按课程分的）
  const { sessions, isPending: sessionsPending } = useRecentChatSessions(
    courses.map((course) => course.id),
    3,
  );

  /**
   * 新建对话：回到首页空态。
   *
   * 只清掉当前会话 id，**保留**最近选择的课程（``activeChatCourseId``），
   * 因此首页会继续预选那门课。刻意不打开右侧面板——首页的对话在中央
   * （开发方案 4.5）。
   */
  function startNewConversation() {
    clearChatSession();
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
              title={`对话 · ${session.activityLabel}`}
              onClick={() => {
                // 进入首页中央会话页：消息在中央显示，不打开右侧抽屉
                openChatSession(session.id, session.courseId);
                navigate(`/chats/${session.id}`);
              }}
            >
              <Icon name="spark" size={12} />
              {/* 契约的 ChatSession 没有标题，用最近活动时间做标识 */}
              <span className={styles.courseLinkText}>对话 · {session.activityLabel}</span>
            </button>
          ))
        )}
      </section>

      <div className={styles.footer}>
        {userMenuOpen && (
          <div className={styles.userMenu}>
            <button type="button" onClick={() => { setSkillsOpen(true); setUserMenuOpen(false); }}>我的 Skill</button>
          </div>
        )}
        <button type="button" className={styles.avatar} onClick={() => setUserMenuOpen(!userMenuOpen)} aria-label="用户菜单">
          {userQuery.data?.initial ?? "·"}
        </button>
        <button type="button" className={styles.userMeta} onClick={() => setUserMenuOpen(!userMenuOpen)}>
          <strong>{userQuery.data?.displayName ?? "加载中"}</strong>
          <span>{userQuery.data?.roleLabel ?? ""}</span>
        </button>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => logout.mutate()}
          disabled={logout.isPending}
          aria-label="退出登录"
          title="退出登录"
        >
          {logout.isPending ? "退出中…" : "退出"}
        </Button>
      </div>
      {skillsOpen && <MySkillsDialog onClose={() => setSkillsOpen(false)} />}
    </aside>
  );
}
