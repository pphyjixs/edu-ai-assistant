/** 练习卡片。用于课程学习页的已发布练习列表与教师草稿列表。 */

import { Link } from "react-router-dom";

import { Pill } from "@/components/Pill/Pill";
import type { PracticeSetSummaryVM } from "@/features/practice/model/types";

import styles from "./PracticeSetCard.module.css";

export type PracticeSetCardProps = {
  practiceSet: PracticeSetSummaryVM;
  courseId: string;
};

export function PracticeSetCard({ practiceSet, courseId }: PracticeSetCardProps) {
  return (
    <Link to={`/courses/${courseId}/learn/${practiceSet.id}`} className={styles.card}>
      <div className={styles.head}>
        <Pill tone={practiceSet.statusTone}>{practiceSet.statusLabel}</Pill>
        <span className={styles.meta}>
          {practiceSet.questionCount} 题 · {practiceSet.difficultyLabel}
        </span>
      </div>

      <h3 className={styles.title}>{practiceSet.title}</h3>

      <div className={styles.foot}>
        <span>{practiceSet.typeLabels.join(" / ")}</span>
        <span>
          {practiceSet.publishedAtLabel
            ? `发布于 ${practiceSet.publishedAtLabel}`
            : `创建于 ${practiceSet.createdAtLabel}`}
        </span>
      </div>
    </Link>
  );
}
