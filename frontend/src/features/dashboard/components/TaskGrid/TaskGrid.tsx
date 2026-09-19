/** 今日待办网格。统一处理加载骨架与空状态。 */

import { EmptyState } from "@/components/EmptyState/EmptyState";
import { Skeleton } from "@/components/Skeleton/Skeleton";
import type { TodayTaskVM } from "@/features/dashboard/model/types";

import { TaskCard } from "../TaskCard/TaskCard";

import styles from "./TaskGrid.module.css";

export type TaskGridProps = {
  tasks: TodayTaskVM[];
  isPending: boolean;
};

export function TaskGrid({ tasks, isPending }: TaskGridProps) {
  if (isPending) {
    return (
      <div className={styles.grid} aria-busy="true">
        <Skeleton height={176} radius="14px" />
        <Skeleton height={176} radius="14px" />
        <Skeleton height={176} radius="14px" />
      </div>
    );
  }

  if (tasks.length === 0) {
    return (
      <EmptyState
        illustration="empty-learning"
        title="暂时没有待办任务"
        description="教师发布实验任务后，会出现在这里。"
      />
    );
  }

  return (
    <div className={styles.grid}>
      {tasks.map((task) => (
        <TaskCard key={task.id} task={task} />
      ))}
    </div>
  );
}
