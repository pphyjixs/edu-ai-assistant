/**
 * 作业详情页头：状态、标题、总分与截止信息
 * （DEVELOPMENT_SPEC 第 14.1 节）。
 *
 * 剩余时间只由真实的 due_at 计算；临近截止用橙色文字提示，
 * 不使用大面积红色告警。
 *
 * 教师的「编辑 / 发布 / 关闭」由页面通过 ``actions`` 传入：
 * 页头只负责版位，不自己判断权限（权限与状态以后端为准，契约 8.1）。
 */

import type { ReactNode } from "react";

import { Pill } from "@/components/Pill/Pill";
import type { AssignmentVM } from "@/features/assignments/model/types";

import styles from "./AssignmentHeader.module.css";

export type AssignmentHeaderProps = {
  assignment: AssignmentVM;
  /** 教师操作区；学生视角不传 */
  actions?: ReactNode;
};

export function AssignmentHeader({ assignment, actions }: AssignmentHeaderProps) {
  return (
    <header className={styles.header}>
      <div className={styles.info}>
        <div className={styles.statusRow}>
          <Pill tone={assignment.statusTone}>{assignment.statusLabel}</Pill>
          <span className={styles.totalScore}>总分 {assignment.totalScore} 分</span>
          {/* 评分规则版本：改标题不产生新版本，改分值才会（契约 8.9） */}
          <span className={styles.totalScore}>评分规则 v{assignment.rubricVersion}</span>
        </div>
        <h1 className={styles.title}>{assignment.title}</h1>
        {actions ? <div className={styles.actions}>{actions}</div> : null}
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
        {assignment.publishedAtLabel ? (
          <span className={styles.lateRule}>发布于 {assignment.publishedAtLabel}</span>
        ) : null}
        {assignment.closedAtLabel ? (
          <span className={styles.lateRule}>关闭于 {assignment.closedAtLabel}</span>
        ) : null}
      </div>
    </header>
  );
}
