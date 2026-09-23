/**
 * 学生：我的成绩与反馈（契约 9.4 / 9.7）。
 *
 * 契约里**没有**"跨作业的成绩列表"接口，因此这里按作业逐条取：
 * `GET /assignments/{id}/submissions` 对学生返回本人 0–1 条，
 * 已发布的再取一次 `GET /submissions/{id}/grade-review`。
 *
 * 代价是 N 次请求（N = 课程里的作业数），但每一格都来自真实接口：
 * 未发布的成绩不会被编出来，批改中 / 待复核是学生可见的真实状态。
 * 批改详情对学生**发布前返回 404**，所以只在 `PUBLISHED` 时才请求成绩。
 */

import { useQueries } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { Card } from "@/components/Card/Card";
import { EmptyState } from "@/components/EmptyState/EmptyState";
import { Pill } from "@/components/Pill/Pill";
import { Skeleton, SkeletonLines } from "@/components/Skeleton/Skeleton";
import { useAssignments } from "@/features/assignments/hooks/useAssignments";
import { useCurrentUser } from "@/features/auth/hooks/useCurrentUser";
import { gradingApi, SUBMISSION_PAGE_SIZE } from "@/features/grading/api";
import { toGradeReviewVM, toSubmissionVM } from "@/features/grading/model/types";
import { queryKeys } from "@/services/queryKeys";
import { formatScore } from "@/utils/format";

import styles from "./GradingPages.module.css";

export function GradesPage() {
  const { courseId } = useParams<{ courseId: string }>();
  const userQuery = useCurrentUser();
  const isTeacher = userQuery.data?.role === "teacher";

  const assignmentsQuery = useAssignments(courseId);
  // 草稿对学生不可见，也不该出现在成绩页
  const assignments = (assignmentsQuery.data?.items ?? []).filter(
    (assignment) => assignment.status !== "draft",
  );

  // 每份作业取"我的提交"：学生只会拿到 0–1 条，未完成的 UPLOADING 不出现
  const submissionQueries = useQueries({
    queries: assignments.map((assignment) => ({
      queryKey: [...queryKeys.submissions(assignment.id), 1] as const,
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        gradingApi.listSubmissions(assignment.id, 1, SUBMISSION_PAGE_SIZE, signal),
      enabled: !isTeacher && Boolean(courseId),
      staleTime: 0,
    })),
  });

  const rows = assignments.map((assignment, index) => {
    const page = submissionQueries[index]?.data;
    const submission = page?.items[0]
      ? toSubmissionVM(page.items[0], { isTeacher: false })
      : null;
    return {
      assignment,
      submission,
      isPending: submissionQueries[index]?.isPending ?? true,
    };
  });

  const publishedSubmissionIds = rows
    .map((row) => row.submission)
    .filter((item): item is NonNullable<typeof item> => item?.status === "PUBLISHED")
    .map((item) => item.id);

  // 只在已发布时取批改详情（发布前对学生是 404）
  const reviewQueries = useQueries({
    queries: publishedSubmissionIds.map((submissionId) => ({
      queryKey: queryKeys.gradeReview(submissionId),
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        gradingApi.gradeReview(submissionId, signal),
      select: toGradeReviewVM,
      staleTime: 0,
      retry: false,
    })),
  });

  const scoreBySubmission = new Map(
    publishedSubmissionIds.map((submissionId, index) => [
      submissionId,
      reviewQueries[index]?.data ?? null,
    ]),
  );

  if (userQuery.isPending || assignmentsQuery.isPending) {
    return (
      <div className={styles.page} aria-busy="true">
        <Skeleton height={26} width="34%" />
        <div className={styles.gap} />
        <Card>
          <SkeletonLines lines={5} />
        </Card>
      </div>
    );
  }

  if (isTeacher) {
    return (
      <div className={styles.page}>
        <header className={styles.header}>
          <div>
            <h1 className={styles.title}>成绩与反馈</h1>
            <p className={styles.subtitle}>这是学生查看自己成绩的页面</p>
          </div>
        </header>
        <Card>
          <p className={styles.notice}>
            教师要看全班的提交与批改，请到 AI 批改工作台。
          </p>
          <p className={styles.notice}>
            <Link className={styles.rowLink} to={`/courses/${courseId}/grading`}>
              前往 AI 批改工作台
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
          <h1 className={styles.title}>成绩与反馈</h1>
          <p className={styles.subtitle}>
            正式成绩由教师发布；发布前这里只显示提交与批改的进度。
          </p>
        </div>
      </header>

      {rows.length === 0 ? (
        <Card>
          <EmptyState
            title="还没有已发布的作业"
            description="教师发布作业后，这里会出现可以提交的任务。"
          />
        </Card>
      ) : (
        <Card>
          <table className={styles.table}>
            <thead>
              <tr>
                <th scope="col">作业</th>
                <th scope="col">我的提交</th>
                <th scope="col">成绩</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ assignment, submission, isPending }) => {
                const review = submission ? scoreBySubmission.get(submission.id) : null;

                return (
                  <tr key={assignment.id}>
                    <td className={styles.rowTitle}>{assignment.title}</td>
                    <td>
                      {isPending ? (
                        "读取中…"
                      ) : submission ? (
                        <Pill tone={submission.statusTone}>{submission.statusLabel}</Pill>
                      ) : (
                        <span>未提交</span>
                      )}
                    </td>
                    <td>
                      {review
                        ? `${formatScore(review.finalTotalScore)} / ${formatScore(review.maxTotal)}`
                        : submission?.scoreVisible
                          ? "读取中…"
                          : "未发布"}
                    </td>
                    <td>
                      {submission ? (
                        <Link
                          className={styles.rowLink}
                          to={`/courses/${courseId}/submissions/${submission.id}`}
                        >
                          {submission.scoreVisible ? "查看成绩" : "查看状态"}
                        </Link>
                      ) : assignment.canSubmit ? (
                        <Link
                          className={styles.rowLink}
                          to={`/courses/${courseId}/assignments/${assignment.id}`}
                        >
                          去提交
                        </Link>
                      ) : (
                        <span>—</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
