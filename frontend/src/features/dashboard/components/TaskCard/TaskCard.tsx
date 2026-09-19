/**
 * 今日待办卡片。整张卡可点击进入任务（DEVELOPMENT_SPEC 第 7.3 节）。
 * 临近截止只用一枚小橙色标签 + 顶部细边，不使用大面积红色告警。
 */

import { Link } from "react-router-dom";

import { Pill } from "@/components/Pill/Pill";
import type { TodayTaskVM } from "@/features/dashboard/model/types";

import styles from "./TaskCard.module.css";

export type TaskCardProps = {
  task: TodayTaskVM;
};

export function TaskCard({ task }: TaskCardProps) {
  const urgent = task.badgeTone === "warn";

  return (
    <Link
      to={task.href}
      className={urgent ? `${styles.card} ${styles.urgent}` : styles.card}
    >
      <div className={styles.meta}>
        <Pill tone={task.badgeTone}>{task.badgeLabel}</Pill>
        <span className={styles.courseName} title={task.courseName}>
          {task.courseName}
        </span>
      </div>

      <h3 className={styles.title}>{task.title}</h3>

      <div className={styles.bottom}>
        <span className={styles.due}>截止 {task.dueLabel}</span>
        <span className={styles.action}>{task.actionLabel} →</span>
      </div>
    </Link>
  );
}
