/**
 * 课程内左侧导航。
 *
 * 教师与学生共用同一套 Workspace，靠角色决定菜单项
 * （DEVELOPMENT_SPEC 第 8.1 节）。菜单只影响展示，
 * 真正的权限边界仍由后端校验。
 */

import { Link, NavLink } from "react-router-dom";

import { Icon, type IconName } from "@/components/Icon/Icon";
import { Skeleton } from "@/components/Skeleton/Skeleton";
import { useCurrentUser } from "@/features/auth/hooks/useCurrentUser";
import type { CourseVM } from "@/features/courses/model/types";

import styles from "./CourseSidebar.module.css";

type NavItem = { to: string; label: string; icon: IconName; end?: boolean };

const STUDENT_NAV: NavItem[] = [
  { to: "", label: "概览", icon: "home", end: true },
  { to: "materials", label: "课程资料", icon: "file" },
  { to: "learn", label: "学习", icon: "workspace" },
  { to: "assignments", label: "作业", icon: "assignment" },
  { to: "grades", label: "成绩", icon: "grade" },
];

const TEACHER_NAV: NavItem[] = [
  { to: "", label: "概览", icon: "home", end: true },
  { to: "manage/materials", label: "课程资料", icon: "file" },
  { to: "manage/members", label: "学生", icon: "courses" },
  { to: "manage/assignments", label: "作业", icon: "assignment" },
  { to: "grading", label: "AI 批改", icon: "spark" },
];

export type CourseSidebarProps = {
  courseId: string;
  course: CourseVM | undefined;
  isLoading: boolean;
};

export function CourseSidebar({ courseId, course, isLoading }: CourseSidebarProps) {
  const userQuery = useCurrentUser();
  const navItems = userQuery.data?.role === "teacher" ? TEACHER_NAV : STUDENT_NAV;
  const basePath = `/courses/${courseId}`;

  return (
    <aside className={styles.sidebar} aria-label="课程导航">
      <div className={styles.brand}>
        <img
          className={styles.brandMark}
          src="/svg/icons/logo-studybuddy.svg"
          alt=""
          width={30}
          height={30}
          aria-hidden="true"
        />
        <span className={styles.brandName}>StudyBuddy</span>
      </div>

      <Link to="/" className={styles.backHome}>
        <Icon name="chevron-left" size={14} />
        返回首页
      </Link>

      <div className={styles.courseTitleBlock}>
        <span className={`${styles.courseColor} ${styles[course?.accent ?? "blue"]}`} />
        <div className={styles.courseTitleText}>
          {isLoading ? (
            <>
              <Skeleton height={12} width="82%" />
              <Skeleton height={10} width="56%" />
            </>
          ) : (
            <>
              <strong title={course?.name}>{course?.name ?? "课程"}</strong>
              <span>{course?.teacherName ?? ""}</span>
            </>
          )}
        </div>
      </div>

      <nav className={styles.nav} aria-label="课程功能">
        {navItems.map((item) => (
          <NavLink
            key={item.label}
            to={item.to ? `${basePath}/${item.to}` : basePath}
            end={item.end}
            className={({ isActive }) =>
              isActive ? `${styles.navItem} ${styles.navItemActive}` : styles.navItem
            }
          >
            <Icon name={item.icon} size={16} />
            <span>{item.label}</span>
          </NavLink>
        ))}
      </nav>

      <section className={styles.section} aria-label="课程内容">
        <p className={styles.sectionLabel}>课程内容</p>
        {/* 章节大纲来自资料解析结果（materials 模块），本阶段尚未实现，
            因此这里只说明结构，不用假数据填充 */}
        <p className={styles.sectionEmpty}>
          课程资料解析完成后，这里会按顺序显示章节大纲与知识点。
        </p>
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
