/**
 * 作业详情页的「提交与批改」区块。
 *
 * 三个视角，三种内容（契约 9.1 的权限表）：
 *
 * - **学生**：自己的提交状态；还没正式提交时可上传报告（PDF / DOCX）；
 *   已出分时给出查看成绩的入口；
 * - **课程创建教师**：收到多少份提交、其中多少待批改 / 待复核，并进入提交列表；
 * - **其他教师成员**：提交接口对其返回 `403 ROLE_FORBIDDEN`，因此只说明原因，
 *   不去打一个注定失败的请求。
 *
 * 「当前是否可提交」用的是契约 8.10 的真实规则（作业状态 + 截止时间 + 是否允许补交），
 * 与提交模块自身的状态机是两件事，这里分别表达。
 */

import { Link } from "react-router-dom";

import { Card } from "@/components/Card/Card";
import { Pill } from "@/components/Pill/Pill";
import { SkeletonLines } from "@/components/Skeleton/Skeleton";
import type { AssignmentVM } from "@/features/assignments/model/types";
import { useCurrentUser } from "@/features/auth/hooks/useCurrentUser";
import { SubmissionCard } from "@/features/grading/components/SubmissionCard/SubmissionCard";
import { SubmissionUploader } from "@/features/grading/components/SubmissionUploader/SubmissionUploader";
import { useSubmissions } from "@/features/grading/hooks/useSubmissions";

import styles from "./SubmissionPanel.module.css";

export type SubmissionPanelProps = {
  assignment: AssignmentVM;
  /** 当前用户是否为课程创建教师 */
  isOwner: boolean;
};

export function SubmissionPanel({ assignment, isOwner }: SubmissionPanelProps) {
  const userQuery = useCurrentUser();
  const role = userQuery.data?.role;

  // 非成员/其他教师拿到 403，因此只对"学生或创建教师"发起查询
  const canQuery = role === "student" || (role === "teacher" && isOwner);
  const submissions = useSubmissions(canQuery ? assignment.id : undefined, {
    isTeacher: isOwner,
  });

  if (role === "teacher" && !isOwner) {
    return (
      <Card>
        <div className={styles.head}>
          <h2 className={styles.title}>提交与批改</h2>
          <Pill tone="neutral">不可管理</Pill>
        </div>
        <p className={styles.state}>
          你不是这门课程的创建教师，因此看不到学生的提交与批改结果。
        </p>
      </Card>
    );
  }

  if (isOwner) {
    const items = submissions.data?.items ?? [];
    const total = submissions.data?.total ?? 0;
    const pending = items.filter((item) => item.status === "SUBMITTED").length;
    const grading = items.filter((item) => item.status === "GRADING").length;
    const toReview = items.filter((item) => item.status === "REVIEW_REQUIRED").length;
    const failed = items.filter((item) => item.status === "FAILED").length;

    return (
      <Card>
        <div className={styles.head}>
          <h2 className={styles.title}>提交与批改</h2>
          <Pill tone={total > 0 ? "info" : "neutral"}>
            {submissions.isPending ? "统计中…" : `已提交 ${total} 份`}
          </Pill>
        </div>

        {submissions.isPending ? (
          <SkeletonLines lines={2} />
        ) : submissions.isError ? (
          <p className={styles.state}>提交统计暂时读不到，进入提交列表可以重试。</p>
        ) : (
          <>
            <p className={styles.state}>
              {total === 0
                ? "还没有学生提交报告。学生上传不会自动触发批改，需要你在这里发起。"
                : "AI 批改不会自动开始：先触发批改，再复核并发布成绩。"}
            </p>
            {total > 0 ? (
              <div className={styles.stats}>
                <span className={styles.stat}>待批改 {pending}</span>
                <span className={styles.stat}>批改中 {grading}</span>
                <span className={styles.stat}>待复核 {toReview}</span>
                {failed > 0 ? (
                  <span className={`${styles.stat} ${styles.statWarn}`}>批改失败 {failed}</span>
                ) : null}
              </div>
            ) : null}
          </>
        )}

        <Link
          className={styles.link}
          to={`/courses/${assignment.courseId}/assignments/${assignment.id}/submissions`}
        >
          查看提交列表
        </Link>
      </Card>
    );
  }

  /* ------------------------------ 学生视角 ------------------------------ */

  const mine = submissions.data?.items[0] ?? null;

  return (
    <Card>
      <div className={styles.head}>
        <h2 className={styles.title}>我的提交</h2>
        {mine ? (
          <Pill tone={mine.statusTone}>{mine.statusLabel}</Pill>
        ) : (
          <Pill tone={assignment.canSubmit ? "info" : "neutral"}>
            {assignment.canSubmit ? "当前可提交" : "当前不可提交"}
          </Pill>
        )}
      </div>

      <p className={styles.state}>{describeStudentState(assignment, Boolean(mine))}</p>

      {submissions.isPending ? (
        <SkeletonLines lines={2} />
      ) : mine ? (
        <SubmissionCard
          submission={mine}
          courseId={assignment.courseId}
          assignmentId={assignment.id}
          canUpload={assignment.canSubmit}
        />
      ) : assignment.canSubmit ? (
        <SubmissionUploader
          assignmentId={assignment.id}
          courseId={assignment.courseId}
        />
      ) : null}
    </Card>
  );
}

function describeStudentState(assignment: AssignmentVM, hasSubmission: boolean): string {
  if (hasSubmission) {
    return "报告已提交。教师发起批改并发布成绩后，这里会出现成绩与评语。";
  }
  if (assignment.status === "draft") {
    return "这份作业还是草稿，学生看不到。";
  }
  if (assignment.status === "closed") {
    // 契约 8.10：手工关闭优先于 allow_late_submission
    return "教师已关闭这份作业的提交入口，不能再提交报告。";
  }
  if (assignment.status === "archived") {
    return "这份作业已归档，不能提交。";
  }
  if (assignment.remaining.expired && !assignment.allowLateSubmission) {
    return "已经过了截止时间，且这份作业不允许补交。";
  }
  if (assignment.remaining.expired) {
    return "已经过了截止时间，但这份作业允许补交，现在提交会标记为补交。";
  }
  return `支持 PDF 与 DOCX 格式，${assignment.remaining.text}。`;
}
