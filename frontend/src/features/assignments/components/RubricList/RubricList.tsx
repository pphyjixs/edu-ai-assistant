/**
 * 评分项列表。
 *
 * 契约 12 有稳定错误码 RUBRIC_SCORE_MISMATCH（评分项合计与总分不一致，422）。
 * 前端在这里提前把不一致显示出来，而不是等提交后才由后端报错。
 */

import { Pill } from "@/components/Pill/Pill";
import { EmptyState } from "@/components/EmptyState/EmptyState";
import type { RubricItemVM } from "@/features/assignments/model/types";

import { RubricItem } from "../RubricItem/RubricItem";

import styles from "./RubricList.module.css";

export type RubricListProps = {
  rubric: RubricItemVM[];
  totalScore: number;
  mismatch: boolean;
};

export function RubricList({ rubric, totalScore, mismatch }: RubricListProps) {
  if (rubric.length === 0) {
    return (
      <EmptyState
        title="这门作业还没有配置评分项"
        description="教师配置评分规则后，这里会显示每个评分项的分值。"
      />
    );
  }

  return (
    <div>
      <div className={styles.summary}>
        <span className={styles.summaryText}>
          共 {rubric.length} 个评分项，合计 {rubric.reduce((sum, item) => sum + item.maxScore, 0)} 分
        </span>
        {mismatch ? (
          <Pill tone="warn">评分项合计与总分不一致</Pill>
        ) : null}
      </div>

      <ul className={styles.list}>
        {rubric.map((item) => (
          <RubricItem key={item.id} item={item} totalScore={totalScore} />
        ))}
      </ul>
    </div>
  );
}
