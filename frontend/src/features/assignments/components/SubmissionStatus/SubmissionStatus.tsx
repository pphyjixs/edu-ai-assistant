/**
 * 我的提交状态。
 *
 * 关键产品规则：AI 的建议与「批改中 / 等待复核」都不是最终成绩，
 * 只有教师发布（PUBLISHED）之后学生看到的才是正式结果。
 * 这里用文字明确区分，避免把建议分误读成成绩。
 */

import { Pill } from "@/components/Pill/Pill";
import type { SubmissionVM } from "@/features/assignments/model/types";

import styles from "./SubmissionStatus.module.css";

export type SubmissionStatusProps = {
  submission: SubmissionVM | null;
};

export function SubmissionStatus({ submission }: SubmissionStatusProps) {
  if (!submission) {
    return (
      <div className={styles.row}>
        <Pill tone="neutral">尚未提交</Pill>
        <span className={styles.hint}>上传实验报告后即可提交。</span>
      </div>
    );
  }

  return (
    <div className={styles.row}>
      <Pill tone={submission.statusTone}>{submission.statusLabel}</Pill>
      <span className={styles.hint}>提交于 {submission.submittedAtLabel}</span>
      {submission.isFinal ? (
        <span className={styles.hint}>教师已发布正式结果。</span>
      ) : (
        <span className={styles.hint}>教师尚未发布，当前结果不是正式成绩。</span>
      )}
    </div>
  );
}
