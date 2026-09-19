/** 单个评分项：名称、说明、满分。 */

import type { RubricItemVM } from "@/features/assignments/model/types";

import styles from "./RubricItem.module.css";

export type RubricItemProps = {
  item: RubricItemVM;
  totalScore: number;
};

export function RubricItem({ item, totalScore }: RubricItemProps) {
  const share = totalScore > 0 ? Math.round((item.maxScore / totalScore) * 100) : 0;

  return (
    <li className={styles.item}>
      <div className={styles.head}>
        <span className={styles.order}>{item.order}</span>
        <span className={styles.title}>{item.title}</span>
        <span className={styles.score}>
          {item.maxScore} 分
          <em className={styles.share}>占 {share}%</em>
        </span>
      </div>
      <p className={styles.description}>{item.description}</p>
    </li>
  );
}
