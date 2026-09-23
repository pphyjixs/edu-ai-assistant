/**
 * 教师：某个作业的提交列表（契约 9.4）。
 *
 * 列表只含正式提交；未完成的 `UPLOADING` 记录不出现（草稿态上传不进正式列表），
 * 因此空列表的文案要说明这一点，避免教师误判成"没人交"。
 *
 * 触发批改在提交详情里做——这里只负责把"谁交了、现在到哪一步"看清楚。
 */

import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { Button } from "@/components/Button/Button";
import { Card } from "@/components/Card/Card";
import { EmptyState } from "@/components/EmptyState/EmptyState";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { Skeleton, SkeletonLines } from "@/components/Skeleton/Skeleton";
import { useAssignment } from "@/features/assignments/hooks/useAssignments";
import { useCurrentUser } from "@/features/auth/hooks/useCurrentUser";
import { GradingErrorNotice } from "@/features/grading/components/GradingErrorNotice/GradingErrorNotice";
import { SubmissionList } from "@/features/grading/components/SubmissionList/SubmissionList";
import { useSubmissions } from "@/features/grading/hooks/useSubmissions";
import { toAppError } from "@/services/http";

import styles from "./GradingPages.module.css";

export function SubmissionsPage() {
  const { courseId, assignmentId } = useParams<{
    courseId: string;
    assignmentId: string;
  }>();
  const [page, setPage] = useState(1);

  const userQuery = useCurrentUser();
  const isTeacher = userQuery.data?.role === "teacher";

  const assignmentQuery = useAssignment(assignmentId);
  const submissions = useSubmissions(assignmentId, { isTeacher, page });

  const backTo = `/courses/${courseId}/assignments/${assignmentId}`;

  if (assignmentQuery.isPending) {
    return (
      <div className={styles.page} aria-busy="true">
        <Skeleton height={26} width="38%" />
        <div className={styles.gap} />
        <Card>
          <SkeletonLines lines={4} />
        </Card>
      </div>
    );
  }

  if (assignmentQuery.isError) {
    const error = toAppError(assignmentQuery.error);
    return (
      <div className={styles.page}>
        <ErrorState
          title="作业加载失败"
          message={error.message}
          requestId={error.requestId}
          onRetry={() => void assignmentQuery.refetch()}
        />
      </div>
    );
  }

  const assignment = assignmentQuery.data;
  const items = submissions.data?.items ?? [];
  const total = submissions.data?.total ?? 0;
  const pageSize = submissions.data?.page_size ?? items.length;
  const hasNext = page * pageSize < total;

  return (
    <div className={styles.page}>
      <div className={styles.breadcrumb}>
        <Link to={backTo} className={styles.back}>
          {assignment.title}
        </Link>
        <span aria-hidden="true">›</span>
      </div>

      <header className={styles.header}>
        <div>
          <h1 className={styles.title}>提交列表</h1>
          <p className={styles.subtitle}>
            共 {total} 份正式提交 · 评分规则版本 v{assignment.rubricVersion}
          </p>
        </div>
      </header>

      <Card>
        {submissions.isPending ? (
          <div aria-busy="true">
            <SkeletonLines lines={4} />
          </div>
        ) : submissions.isError ? (
          <GradingErrorNotice
            error={submissions.error}
            title="提交列表加载失败"
            forbiddenMessage="你不是这门课程的创建教师，看不到学生的提交与批改结果。"
            onRetry={() => void submissions.refetch()}
          />
        ) : items.length === 0 ? (
          <EmptyState
            title="还没有收到提交"
            description="学生上传报告并确认后才会出现在这里；只上传了一半的不会计入。"
          />
        ) : (
          <>
            <SubmissionList courseId={courseId as string} items={items} />

            {total > pageSize ? (
              <div className={styles.pager}>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={page === 1}
                  onClick={() => setPage((current) => Math.max(1, current - 1))}
                >
                  上一页
                </Button>
                <span className={styles.pagerText}>第 {page} 页</span>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={!hasNext}
                  onClick={() => setPage((current) => current + 1)}
                >
                  下一页
                </Button>
              </div>
            ) : null}
          </>
        )}
      </Card>
    </div>
  );
}
