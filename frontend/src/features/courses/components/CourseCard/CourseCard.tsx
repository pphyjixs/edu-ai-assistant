/**
 * 课程卡片。
 *
 * 只展示后端确实提供的信息：课程名与课程说明。
 * 契约的 CourseSummary 既没有学习进度、也没有教师姓名（只有 teacher_id），
 * 因此这里不做进度条，也不显示教师名（DEVELOPMENT_SPEC 第 7.4 节）。
 */

import { Link } from "react-router-dom";

import { Pill } from "@/components/Pill/Pill";
import type { CourseVM } from "@/features/courses/model/types";

import styles from "./CourseCard.module.css";

export type CourseCardProps = {
  course: CourseVM;
};

export function CourseCard({ course }: CourseCardProps) {
  return (
    <Link to={`/courses/${course.id}`} className={styles.card}>
      <div className={`${styles.cover} ${styles[course.accent]}`}>
        <img src={`/svg/illustrations/${course.cover}.svg`} alt="" aria-hidden="true" />
      </div>

      <div className={styles.info}>
        <div className={styles.nameRow}>
          <h3 className={styles.name} title={course.name}>
            {course.name}
          </h3>
          {course.status === "archived" ? <Pill tone="neutral">已归档</Pill> : null}
          {course.isOwner ? <Pill tone="info">我创建的</Pill> : null}
        </div>
        <p className={styles.description}>{course.description || "这门课程还没有填写说明。"}</p>
      </div>
    </Link>
  );
}
