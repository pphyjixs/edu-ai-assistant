/**
 * 作业详情页头：状态、标题、总分与截止信息
 * （DEVELOPMENT_SPEC 第 14.1 节）。
 *
 * 剩余时间只由真实的 due_at 计算；临近截止用橙色文字提示，
 * 不使用大面积红色告警。
 */

import { Pill } from "@/components/Pill/Pill";
import type { AssignmentVM } from "@/features/assignments/model/types";

import styles from "./AssignmentHeader.module.css";

export type AssignmentHeaderProps = {
  assignment: AssignmentVM;
};

export function AssignmentHeader({ assignment }: AssignmentHeaderProps) {
  return (
    <header className={styles.header}>
      <div className={styles.info}>
        <div className={styles.statusRow}>
          <Pill tone={assignment.statusTone}>{assignment.statusLabel}</Pill>
          <span className={styles.totalScore}>总分 {assignment.totalScore} 分</span>
        </div>
        <h1 className={styles.title}>{assignment.title}</h1>
      </div>

      <div className={styles.deadlineCard}>
        <span className={styles.deadlineLabel}>截止时间</span>
        <strong className={styles.deadlineValue}>{assignment.dueLabel}</strong>
        <em
          className={
            assignment.remaining.urgent
              ? `${styles.remaining} ${styles.remainingUrgent}`
              : styles.remaining
          }
        >
          {assignment.remaining.text}
        </em>
        <span className={styles.lateRule}>
          {assignment.allowLateSubmission ? "允许补交" : "不允许补交"}
        </span>
      </div>
    </header>
  );
}
