/**
 * 教师：AI 批改工作台（契约 9.4 / 9.6 / 9.8 / 9.9）。
 *
 * 契约没有"跨作业的待批改列表"接口，因此这里按作业聚合：
 * 每份已发布作业取一次 `GET /assignments/{id}/submissions`（教师视角是全班），
 * 再把需要教师动手的提交挑出来排在最前面。
 *
 * 排序体现的正是教师的工作顺序：**待复核 → 待批改 → 批改失败 → 批改中**，
 * 已发布的只在计数里体现，不占用注意力。
 */

import { useQueries } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { Card } from "@/components/Card/Card";
import { EmptyState } from "@/components/EmptyState/EmptyState";
import { Pill } from "@/components/Pill/Pill";
import { Skeleton, SkeletonLines } from "@/components/Skeleton/Skeleton";
import { useAssignments } from "@/features/assignments/hooks/useAssignments";
import { useCurrentUser } from "@/features/auth/hooks/useCurrentUser";
import { gradingApi } from "@/features/grading/api";
import {
  toSubmissionVM,
  type SubmissionStage,
  type SubmissionVM,
} from "@/features/grading/model/types";
import { queryKeys } from "@/services/queryKeys";

import styles from "./GradingPages.module.css";

/** 教师需要处理的状态，按紧急程度排序 */
const WORK_ORDER: SubmissionStage[] = ["reviewRequired", "submitted", "failed", "grading"];

const WORK_LABEL: Record<string, string> = {
  reviewRequired: "待复核",
  submitted: "待批改",
  failed: "批改失败",
  grading: "批改中",
};

export function GradingWorkbenchPage() {
  const { courseId } = useParams<{ courseId: string }>();
  const userQuery = useCurrentUser();
  const isTeacher = userQuery.data?.role === "teacher";

  const assignmentsQuery = useAssignments(courseId);
  // 草稿学生看不到，也就不可能有提交
  const assignments = (assignmentsQuery.data?.items ?? []).filter(
    (assignment) => assignment.status !== "draft",
  );

  const submissionQueries = useQueries({
    queries: assignments.map((assignment) => ({
      queryKey: [...queryKeys.submissions(assignment.id), 1] as const,
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        gradingApi.listSubmissions(assignment.id, 1, 100, signal),
      enabled: isTeacher && Boolean(courseId),
      staleTime: 0,
    })),
  });

  const groups = assignments.map((assignment, index) => {
    const page = submissionQueries[index]?.data;
    const items = (page?.items ?? []).map((item) =>
      toSubmissionVM(item, { isTeacher: true }),
    );
    const pending = items
      .filter((item) => WORK_ORDER.includes(item.stage))
      .sort(
        (a, b) => WORK_ORDER.indexOf(a.stage) - WORK_ORDER.indexOf(b.stage),
      );

    return {
      assignment,
      total: page?.total ?? 0,
      items,
      pending,
      isPending: submissionQueries[index]?.isPending ?? true,
      isError: submissionQueries[index]?.isError ?? false,
    };
  });

  const totals = groups.reduce(
    (acc, group) => {
      acc.submitted += group.items.filter((item) => item.stage === "submitted").length;
      acc.grading += group.items.filter((item) => item.stage === "grading").length;
      acc.toReview += group.items.filter((item) => item.stage === "reviewRequired").length;
      acc.published += group.items.filter((item) => item.stage === "published").length;
      acc.failed += group.items.filter((item) => item.stage === "failed").length;
      acc.all += group.total;
      return acc;
    },
    { submitted: 0, grading: 0, toReview: 0, published: 0, failed: 0, all: 0 },
  );

  if (userQuery.isPending || assignmentsQuery.isPending) {
    return (
      <div className={styles.page} aria-busy="true">
        <Skeleton height={26} width="36%" />
        <div className={styles.gap} />
        <Card>
          <SkeletonLines lines={5} />
        </Card>
      </div>
    );
  }

  if (!isTeacher) {
    return (
      <div className={styles.page}>
        <header className={styles.header}>
          <div>
            <h1 className={styles.title}>AI 批改</h1>
            <p className={styles.subtitle}>这是教师管理全班提交的页面</p>
          </div>
        </header>
        <Card>
          <p className={styles.notice}>
            你的成绩在「成绩与反馈」里查看；AI 批改与发布由教师完成。
          </p>
          <p className={styles.notice}>
            <Link className={styles.rowLink} to={`/courses/${courseId}/grades`}>
              前往成绩与反馈
            </Link>
          </p>
        </Card>
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <h1 className={styles.title}>AI 批改</h1>
          <p className={styles.subtitle}>
            上传报告不会自动触发批改：需要你触发批改、复核，然后发布成绩。
          </p>
        </div>
      </header>

      <Card className={styles.card}>
        <div className={styles.summaryRow}>
          <Stat label="待复核" value={totals.toReview} tone="warn" />
          <Stat label="待批改" value={totals.submitted} tone="info" />
          <Stat label="批改中" value={totals.grading} tone="info" />
          <Stat label="批改失败" value={totals.failed} tone="danger" />
          <Stat label="已发布" value={totals.published} tone="success" />
          <Stat label="提交总数" value={totals.all} tone="neutral" />
        </div>
      </Card>

      {groups.length === 0 ? (
        <Card>
          <EmptyState
            title="还没有可批改的作业"
            description="先发布作业，学生提交报告后这里会出现待批改的记录。"
          />
        </Card>
      ) : (
        groups.map((group) => (
          <Card key={group.assignment.id} className={styles.card}>
            <div className={styles.detailHead}>
              <div className={styles.detailMain}>
                <span className={styles.detailFilename}>{group.assignment.title}</span>
                <span className={styles.detailMeta}>
                  {group.total} 份提交 · 评分版本 v{group.assignment.rubricVersion}
                  {group.assignment.status === "closed" ? " · 已关闭提交" : ""}
                  {group.assignment.status === "archived" ? " · 已归档（只读）" : ""}
                </span>
              </div>
              <Link
                className={styles.rowLink}
                to={`/courses/${courseId}/assignments/${group.assignment.id}/submissions`}
              >
                查看全部提交
              </Link>
            </div>

            {group.isPending ? (
              <div aria-busy="true">
                <SkeletonLines lines={2} />
              </div>
            ) : group.isError ? (
              <p className={styles.notice}>这一份的提交暂时读不到，稍后重试。</p>
            ) : group.pending.length === 0 ? (
              <p className={styles.notice}>
                {group.total === 0
                  ? "还没有学生提交。"
                  : "这一份暂时没有需要处理的提交（都已发布或等待批改结果）。"}
              </p>
            ) : (
              <ul className={styles.workList}>
                {group.pending.slice(0, 8).map((item) => (
                  <WorkRow
                    key={item.id}
                    courseId={courseId as string}
                    submission={item}
                  />
                ))}
                {group.pending.length > 8 ? (
                  <li className={styles.notice}>
                    还有 {group.pending.length - 8} 份，进入提交列表查看。
                  </li>
                ) : null}
              </ul>
            )}
          </Card>
        ))
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "neutral" | "info" | "warn" | "success" | "danger";
}) {
  return (
    <div className={styles.statCard}>
      <Pill tone={value > 0 ? tone : "neutral"}>{label}</Pill>
      <span className={styles.statValue}>{value}</span>
    </div>
  );
}

function WorkRow({ courseId, submission }: { courseId: string; submission: SubmissionVM }) {
  return (
    <li className={styles.workItem}>
      <div className={styles.workMain}>
        <Pill tone={submission.statusTone}>
          {WORK_LABEL[submission.stage] ?? submission.statusLabel}
        </Pill>
        <span className={styles.detailMeta}>
          学生 {submission.studentId.slice(0, 8)} · {submission.filename}
          {submission.submittedAtLabel ? ` · 提交于 ${submission.submittedAtLabel}` : ""}
        </span>
      </div>
      <Link className={styles.rowLink} to={`/courses/${courseId}/submissions/${submission.id}`}>
        {submission.stage === "reviewRequired" ? "去复核" : "去处理"}
      </Link>
    </li>
  );
}
