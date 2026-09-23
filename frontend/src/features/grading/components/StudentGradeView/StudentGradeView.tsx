/**
 * 学生看到的成绩（契约 9.7）。
 *
 * 只展示**终稿**：`final_total_score`、教师总结、教师评语与证据；
 * AI 原始建议分对学生在响应里就是 `null`，界面上因此不会出现"AI 给分"
 * 之类的字样，避免把未发布的草稿当成正式成绩。
 */

import { Pill } from "@/components/Pill/Pill";
import { GradeItemCard } from "@/features/grading/components/GradeItemCard/GradeItemCard";
import type { GradeReviewVM } from "@/features/grading/model/types";
import { formatScore } from "@/utils/format";

import styles from "./StudentGradeView.module.css";

export type StudentGradeViewProps = {
  review: GradeReviewVM;
  assignmentTitle?: string;
};

export function StudentGradeView({ review, assignmentTitle }: StudentGradeViewProps) {
  return (
    <div className={styles.wrap}>
      <div className={styles.scoreCard}>
        <div className={styles.scoreMain}>
          <span className={styles.scoreValue}>{formatScore(review.finalTotalScore)}</span>
          <span className={styles.scoreMax}>/ {formatScore(review.maxTotal)}</span>
        </div>
        <div className={styles.scoreMeta}>
          <Pill tone="success">教师已发布</Pill>
          {review.publishedAtLabel ? (
            <span className={styles.publishedAt}>发布于 {review.publishedAtLabel}</span>
          ) : null}
        </div>
      </div>

      {assignmentTitle ? <p className={styles.assignment}>{assignmentTitle}</p> : null}

      {review.teacherSummary ? (
        <div className={styles.summary}>
          <h3 className={styles.summaryTitle}>教师总结</h3>
          <p className={styles.summaryText}>{review.teacherSummary}</p>
        </div>
      ) : null}

      <div className={styles.items}>
        {review.items.map((item) => (
          <GradeItemCard key={item.id} item={item} mode="student" />
        ))}
      </div>
    </div>
  );
}
