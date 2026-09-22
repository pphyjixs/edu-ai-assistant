/**
 * 「我的提交」区块。
 *
 * ⚠️ 提交与批改依赖契约第 9 节的 Submission / Grading 模块，后端**尚未实现**
 * （见 docs/local-development-agent-backend.md 第 3.1 节）。因此这里不提供
 * 上传入口，也不显示任何示例成绩——避免让用户对着一个假的上传按钮操作。
 *
 * 展示的「当前是否可提交」是契约 8.10 的真实规则推导出来的，
 * 属于作业状态本身的信息，不是伪造的提交状态。
 */

import { Card } from "@/components/Card/Card";
import { Pill } from "@/components/Pill/Pill";
import type { AssignmentVM } from "@/features/assignments/model/types";

import styles from "./SubmissionPanel.module.css";

export type SubmissionPanelProps = {
  assignment: AssignmentVM;
  /** 当前用户是否为课程创建教师 */
  isOwner: boolean;
};

export function SubmissionPanel({ assignment, isOwner }: SubmissionPanelProps) {
  return (
    <Card>
      <div className={styles.head}>
        <h2 className={styles.title}>我的提交</h2>
        <Pill tone={assignment.canSubmit ? "info" : "neutral"}>
          {assignment.canSubmit ? "当前可提交" : "当前不可提交"}
        </Pill>
      </div>

      <p className={styles.state}>{describeState(assignment, isOwner)}</p>

      <div className={styles.notice}>
        <p className={styles.noticeTitle}>提交与批改尚未开放</p>
        <p className={styles.noticeText}>
          报告上传、AI 批改、教师复核与成绩发布属于契约第 9 节的 Submission / Grading 模块，
          后端还没实现。这一块会等该模块落地后接入，届时正式成绩只会来自教师的发布结果。
        </p>
      </div>
    </Card>
  );
}

function describeState(assignment: AssignmentVM, isOwner: boolean): string {
  if (isOwner) {
    return "这是你创建的作业。学生提交情况会在提交模块完成后出现在这里。";
  }
  if (assignment.status === "draft") {
    return "这份作业还是草稿，学生看不到。";
  }
  if (assignment.status === "closed") {
    // 契约 8.10：手工关闭优先于 allow_late_submission
    return "教师已关闭这份作业的提交入口。";
  }
  if (assignment.status === "archived") {
    return "这份作业已归档，不能提交。";
  }
  if (assignment.remaining.expired && !assignment.allowLateSubmission) {
    return "已经过了截止时间，且这份作业不允许补交。";
  }
  if (assignment.remaining.expired) {
    return "已经过了截止时间，但这份作业允许补交。";
  }
  return `截止时间前均可提交，${assignment.remaining.text}。`;
}
