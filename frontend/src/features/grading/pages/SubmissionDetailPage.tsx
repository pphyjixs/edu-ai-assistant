/**
 * 提交详情：教师在这里批改与复核，学生在这里看状态与成绩。
 *
 * 角色的分流由**接口本身**决定（契约 9.1）：教师能触发批改、复核与发布，
 * 学生只能读自己的提交；学生的批改详情在成绩发布前一律 `404`。
 * 因此页面不自己判断"能不能看"，而是按状态决定要不要请求批改详情，
 * 并把 409/404 当成正常状态展示。
 *
 * 进度恢复：批改期间除轮询刚创建的任务外，提交详情本身也每 3 秒重取
 * （`pollWhileGrading`），刷新页面后状态照样能自己推进。
 */

import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { Button } from "@/components/Button/Button";
import { Card } from "@/components/Card/Card";
import { Pill } from "@/components/Pill/Pill";
import { Skeleton, SkeletonLines } from "@/components/Skeleton/Skeleton";
import { useAssignment } from "@/features/assignments/hooks/useAssignments";
import { useCurrentUser } from "@/features/auth/hooks/useCurrentUser";
import { GradingErrorNotice } from "@/features/grading/components/GradingErrorNotice/GradingErrorNotice";
import { GradeReviewPanel } from "@/features/grading/components/GradeReviewPanel/GradeReviewPanel";
import { StudentGradeView } from "@/features/grading/components/StudentGradeView/StudentGradeView";
import { useGradeReview } from "@/features/grading/hooks/useGradeReview";
import { useGradeSubmission, useSubmission } from "@/features/grading/hooks/useSubmissions";
import { isTerminalJobStatus, useJobPolling } from "@/services/jobs";
import { toAppError } from "@/services/http";

import styles from "./GradingPages.module.css";

export function SubmissionDetailPage() {
  const { courseId, submissionId } = useParams<{
    courseId: string;
    submissionId: string;
  }>();

  const userQuery = useCurrentUser();
  const isTeacher = userQuery.data?.role === "teacher";

  const submissionQuery = useSubmission(submissionId, {
    isTeacher,
    pollWhileGrading: true,
  });
  const submission = submissionQuery.data;
  const assignmentQuery = useAssignment(submission?.assignmentId);

  // 刚触发的批改任务：只用来显示进度百分比；状态推进靠提交详情自身的轮询
  const [jobId, setJobId] = useState<string | null>(null);
  const jobQuery = useJobPolling(jobId ?? undefined);
  const grade = useGradeSubmission(submission?.assignmentId ?? "");

  // 任务到达终态后收尾：重新拉一次提交与批改详情
  useEffect(() => {
    if (!jobId || !isTerminalJobStatus(jobQuery.data?.status)) return;
    setJobId(null);
    void submissionQuery.refetch();
  }, [jobId, jobQuery.data?.status, submissionQuery]);

  const reviewEnabled =
    submission !== undefined &&
    (submission.status === "REVIEW_REQUIRED" || submission.status === "PUBLISHED");

  const reviewQuery = useGradeReview(submissionId, { enabled: reviewEnabled });

  if (userQuery.isPending || submissionQuery.isPending) {
    return (
      <div className={styles.page} aria-busy="true">
        <Skeleton height={26} width="40%" />
        <div className={styles.gap} />
        <Card>
          <SkeletonLines lines={5} />
        </Card>
      </div>
    );
  }

  if (submissionQuery.isError) {
    return (
      <div className={styles.page}>
        <Card>
          <GradingErrorNotice
            error={submissionQuery.error}
            title="提交加载失败"
            forbiddenMessage="这份提交不属于你，或者你不是这门课程的创建教师。"
            notFoundMessage="这份提交不存在，或者你没有查看权限。"
            onRetry={() => void submissionQuery.refetch()}
          />
        </Card>
      </div>
    );
  }

  if (!submission) {
    return (
      <div className={styles.page}>
        <Card>
          <p className={styles.notice}>没有找到这份提交。</p>
        </Card>
      </div>
    );
  }

  const backTo = `/courses/${courseId}/assignments/${submission.assignmentId}`;
  const gradeError = grade.isError ? toAppError(grade.error) : null;
  const jobInFlight =
    jobQuery.data !== undefined && !isTerminalJobStatus(jobQuery.data.status);
  const grading = submission.stage === "grading" || jobInFlight;

  return (
    <div className={styles.page}>
      <div className={styles.breadcrumb}>
        <Link to={backTo} className={styles.back}>
          {assignmentQuery.data?.title ?? "作业"}
        </Link>
        <span aria-hidden="true">›</span>
        {isTeacher ? (
          <Link
            to={`/courses/${courseId}/assignments/${submission.assignmentId}/submissions`}
            className={styles.back}
          >
            提交列表
          </Link>
        ) : null}
      </div>

      <Card className={styles.card}>
        <div className={styles.detailHead}>
          <div className={styles.detailMain}>
            <span className={styles.detailFilename} title={submission.filename}>
              {submission.filename}
            </span>
            <span className={styles.detailMeta}>
              {submission.typeLabel} · {submission.sizeLabel}
              {submission.submittedAtLabel
                ? ` · 提交于 ${submission.submittedAtLabel}`
                : " · 尚未正式提交"}
              {submission.rubricVersion !== null
                ? ` · 按评分版本 v${submission.rubricVersion}`
                : ""}
            </span>
            <span className={styles.statusLine}>
              <Pill tone={submission.statusTone}>{submission.statusLabel}</Pill>
              {submission.isLateLabel ? <Pill tone="warn">{submission.isLateLabel}</Pill> : null}
            </span>
            <span className={styles.statusHint}>{submission.statusHint}</span>
          </div>

          <div className={styles.detailActions}>
            {submission.downloadUrl ? (
              <a
                className={styles.back}
                href={submission.downloadUrl}
                target="_blank"
                rel="noreferrer"
              >
                下载报告
              </a>
            ) : null}

            {isTeacher && submission.canGrade && !grading ? (
              <Button
                variant="primary"
                size="sm"
                disabled={grade.isPending}
                onClick={() =>
                  grade.mutate(submission.id, {
                    onSuccess: (job) => setJobId(job.id),
                  })
                }
              >
                {grade.isPending
                  ? "提交中…"
                  : submission.status === "FAILED"
                    ? "重试 AI 批改"
                    : "触发 AI 批改"}
              </Button>
            ) : null}
          </div>
        </div>

        {grading ? (
          <div className={styles.statusLine} role="status">
            <Pill tone="info">批改中</Pill>
            <span className={styles.statusHint}>
              AI 正在按提交固定的评分规则版本批改
              {jobQuery.data ? `（进度 ${jobQuery.data.progress}%）` : ""}。
              可以离开这个页面，结果会自动保存。
            </span>
          </div>
        ) : null}

        {gradeError ? (
          <p className={styles.notice} role="alert">
            {gradeError.message}
          </p>
        ) : null}

        {isTeacher && submission.stage === "failed" && !grading ? (
          <p className={styles.notice}>
            上一次批改没有成功，失败不会留下半份草稿；可以直接重试。
          </p>
        ) : null}
      </Card>

      {/* ------------------------------ 批改结果 ------------------------------ */}

      {reviewEnabled ? (
        reviewQuery.isPending ? (
          <Card className={styles.card} aria-busy="true">
            <SkeletonLines lines={6} />
          </Card>
        ) : reviewQuery.isError ? (
          <Card className={styles.card}>
            <ReviewError error={reviewQuery.error} isTeacher={isTeacher} />
          </Card>
        ) : reviewQuery.data ? (
          isTeacher ? (
            <GradeReviewPanel
              review={reviewQuery.data}
              submissionId={submission.id}
              assignmentId={submission.assignmentId}
            />
          ) : (
            <Card className={styles.card}>
              <StudentGradeView
                review={reviewQuery.data}
                assignmentTitle={assignmentQuery.data?.title}
              />
            </Card>
          )
        ) : null
      ) : isTeacher ? (
        <Card className={styles.card}>
          <p className={styles.notice}>
            {submission.stage === "submitted"
              ? "报告已提交但还没有批改结果。点击「触发 AI 批改」开始评分。"
              : "暂时没有可复核的批改结果。"}
          </p>
        </Card>
      ) : (
        <Card className={styles.card}>
          <p className={styles.notice}>
            成绩还没有发布。教师完成复核并发布后，这里会显示最终分数、评语与依据。
          </p>
        </Card>
      )}
    </div>
  );
}

/**
 * 批改详情的错误分流。
 *
 * `409 SUBMISSION_NOT_READY`（批改还没生成）、`502 AI_JOB_FAILED`（任务失败）
 * 都是可解释的正常结果，不能写成"加载失败"。
 */
function ReviewError({ error, isTeacher }: { error: unknown; isTeacher: boolean }) {
  const appError = toAppError(error);

  if (appError.code === "RESOURCE_NOT_FOUND") {
    return (
      <p className={styles.notice}>
        {isTeacher ? "这份提交还没有批改详情。" : "成绩尚未发布，所以看不到批改详情。"}
      </p>
    );
  }

  if (appError.code === "SUBMISSION_NOT_READY") {
    return <p className={styles.notice}>批改还没有生成，稍后再看。</p>;
  }

  if (appError.code === "AI_JOB_FAILED") {
    return (
      <p className={styles.notice}>
        {isTeacher
          ? "上一次批改任务失败了，可以重试；失败不会留下半份草稿。"
          : "批改没有完成，请等教师重试。"}
      </p>
    );
  }

  return <p className={styles.notice}>{appError.message}</p>;
}
