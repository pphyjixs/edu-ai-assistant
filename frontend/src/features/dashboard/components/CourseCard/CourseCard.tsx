/**
 * 课程卡片。
 *
 * 只展示后端确实提供的信息：课程名、教师、课程说明。
 * 契约与核心对象都没有学习进度字段，因此这里不做进度条，
 * 也不用假百分比填充版面（DEVELOPMENT_SPEC 第 7.4 节）。
 */

import { Link } from "react-router-dom";

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
        <h3 className={styles.name} title={course.name}>
          {course.name}
        </h3>
        <p className={styles.teacher}>{course.teacherName}</p>
        <p className={styles.description}>{course.description}</p>
      </div>
    </Link>
  );
}
