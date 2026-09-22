/** 作业列表中的一行。 */

import { Link } from "react-router-dom";

import { Pill } from "@/components/Pill/Pill";
import type { AssignmentVM } from "@/features/assignments/model/types";

import styles from "./AssignmentRow.module.css";

export type AssignmentRowProps = {
  assignment: AssignmentVM;
  courseId: string;
};

export function AssignmentRow({ assignment, courseId }: AssignmentRowProps) {
  const urgent = assignment.status === "published" && assignment.remaining.urgent;

  return (
    <li className={urgent ? `${styles.row} ${styles.urgent}` : styles.row}>
      <div className={styles.main}>
        <div className={styles.titleRow}>
          <Pill tone={assignment.statusTone}>{assignment.statusLabel}</Pill>
          <Link
            className={styles.title}
            to={`/courses/${courseId}/assignments/${assignment.id}`}
            title={assignment.title}
          >
            {assignment.title}
          </Link>
        </div>
        <div className={styles.meta}>
          <span>总分 {assignment.totalScore} 分</span>
          <span>截止 {assignment.dueLabel}</span>
          <span className={urgent ? styles.remainingUrgent : undefined}>
            {assignment.remaining.text}
          </span>
          {assignment.allowLateSubmission ? <span>允许补交</span> : null}
          <span>评分规则 v{assignment.rubricVersion}</span>
        </div>
      </div>

      <Link className={styles.linkButton} to={`/courses/${courseId}/assignments/${assignment.id}`}>
        查看详情
      </Link>
    </li>
  );
}
