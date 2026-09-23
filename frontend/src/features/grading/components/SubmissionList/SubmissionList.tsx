/**
 * 教师看到的提交列表（契约 9.4）。
 *
 * 列表只包含**正式提交**（`submitted_at` 非空）：学生还在上传的草稿态不会出现，
 * 因此这里的条数与学生"我以为我交了"的直觉可能不一致——空列表时文案要说明
 * 「学生上传完成后才会出现在这里」，避免教师以为没人交。
 *
 * 每行给出「当前能做什么」：待批改/失败 → 进入详情触发批改；待复核 → 进入详情复核。
 */

import { Link } from "react-router-dom";

import { Pill } from "@/components/Pill/Pill";
import type { SubmissionVM } from "@/features/grading/model/types";

import styles from "./SubmissionList.module.css";

export type SubmissionListProps = {
  courseId: string;
  items: SubmissionVM[];
};

/** 学生短标识：UUID 太长，展示前 8 位便于对照，不暴露更多信息 */
function shortId(studentId: string): string {
  return studentId.slice(0, 8);
}

export function SubmissionList({ courseId, items }: SubmissionListProps) {
  return (
    <ul className={styles.list}>
      {items.map((item) => (
        <li key={item.id} className={styles.row}>
          <div className={styles.main}>
            <div className={styles.line}>
              <span className={styles.filename} title={item.filename}>
                {item.filename}
              </span>
              <div className={styles.pills}>
                {item.isLateLabel ? <Pill tone="warn">{item.isLateLabel}</Pill> : null}
                <Pill tone={item.statusTone}>{item.statusLabel}</Pill>
              </div>
            </div>
            <span className={styles.meta}>
              学生 {shortId(item.studentId)} · {item.typeLabel} · {item.sizeLabel}
              {item.submittedAtLabel ? ` · 提交于 ${item.submittedAtLabel}` : ""}
              {item.rubricVersion !== null ? ` · 评分版本 v${item.rubricVersion}` : ""}
            </span>
            <span className={styles.hint}>{item.statusHint}</span>
          </div>

          <Link className={styles.action} to={`/courses/${courseId}/submissions/${item.id}`}>
            {item.canReview ? "去复核" : item.canGrade ? "去批改" : "查看"}
          </Link>
        </li>
      ))}
    </ul>
  );
}
