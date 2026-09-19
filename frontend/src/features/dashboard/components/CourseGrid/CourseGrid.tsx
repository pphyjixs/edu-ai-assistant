/** 课程卡片网格。统一处理加载骨架与空状态。 */

import { EmptyState } from "@/components/EmptyState/EmptyState";
import { Skeleton } from "@/components/Skeleton/Skeleton";
import type { CourseVM } from "@/features/courses/model/types";

import { CourseCard } from "../CourseCard/CourseCard";

import styles from "./CourseGrid.module.css";

export type CourseGridProps = {
  courses: CourseVM[];
  isPending: boolean;
};

export function CourseGrid({ courses, isPending }: CourseGridProps) {
  if (isPending) {
    return (
      <div className={styles.grid} aria-busy="true">
        <Skeleton height={300} radius="14px" />
        <Skeleton height={300} radius="14px" />
        <Skeleton height={300} radius="14px" />
      </div>
    );
  }

  if (courses.length === 0) {
    return (
      <EmptyState
        illustration="empty-learning"
        title="还没有课程"
        description="用教师提供的邀请码加入课程后，课程会出现在这里。"
      />
    );
  }

  return (
    <div className={styles.grid}>
      {courses.map((course) => (
        <CourseCard key={course.id} course={course} />
      ))}
    </div>
  );
}
