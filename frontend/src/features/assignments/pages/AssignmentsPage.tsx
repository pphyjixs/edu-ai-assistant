/**
 * 课程作业列表。
 *
 * 教师看到全部状态并可创建任务；学生只看得到 `PUBLISHED`/`CLOSED`/`ARCHIVED`
 * （草稿由后端在查询层排除，学生读草稿详情会得到 404，契约 8.1 / 8.3）。
 */

import { Card } from "@/components/Card/Card";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { Skeleton } from "@/components/Skeleton/Skeleton";
import { useState } from "react";
import { useParams } from "react-router-dom";

import { AssignmentForm } from "@/features/assignments/components/AssignmentForm/AssignmentForm";
import { AssignmentList } from "@/features/assignments/components/AssignmentList/AssignmentList";
import { useAssignments, useCreateAssignment } from "@/features/assignments/hooks/useAssignments";
import { useSetBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { useCourse } from "@/features/courses/hooks/useCourses";
import { toAppError } from "@/services/http";

import styles from "./AssignmentsPage.module.css";

export function AssignmentsPage() {
  const { courseId } = useParams<{ courseId: string }>();
  const courseQuery = useCourse(courseId);
  const assignmentsQuery = useAssignments(courseId);
  const createAssignment = useCreateAssignment(courseId ?? "");
  const [creating, setCreating] = useState(false);

  useSetBuddyContext({ courseId, entityType: "course", entityId: courseId, route: "" });

  const isOwner = Boolean(courseQuery.data?.isOwner);
  const readOnly = courseQuery.data?.status === "archived";
  const listError = assignmentsQuery.isError ? toAppError(assignmentsQuery.error) : null;

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <h1 className={styles.title}>实验任务</h1>
          <p className={styles.subtitle}>
            {assignmentsQuery.isSuccess
              ? `共 ${assignmentsQuery.data.total} 个任务`
              : "正在读取任务列表…"}
            {readOnly ? " · 课程已归档，任务保持可读" : ""}
          </p>
        </div>
        {isOwner && !readOnly && !creating ? (
          <button type="button" className={styles.primaryButton} onClick={() => setCreating(true)}>
            新建任务
          </button>
        ) : null}
      </header>

      {isOwner && !readOnly && creating ? (
        <Card className={styles.formCard}>
          <h2 className={styles.formTitle}>新建实验任务</h2>
          <p className={styles.formHint}>
            创建后是草稿，确认无误再发布；学生看不到草稿。评分项分值合计必须等于总分。
          </p>
          <AssignmentForm
            isPending={createAssignment.isPending}
            error={createAssignment.error}
            onSubmitCreate={(body) =>
              createAssignment.mutate(body, { onSuccess: () => setCreating(false) })
            }
            onCancel={() => setCreating(false)}
          />
        </Card>
      ) : null}

      {courseQuery.isPending ? (
        <Skeleton height={120} radius="14px" />
      ) : courseQuery.isError ? (
        <ErrorState
          title="课程加载失败"
          message={toAppError(courseQuery.error).message}
          onRetry={() => void courseQuery.refetch()}
        />
      ) : (
        <AssignmentList
          assignments={assignmentsQuery.data?.items ?? []}
          isPending={assignmentsQuery.isPending}
          error={listError}
          onRetry={() => void assignmentsQuery.refetch()}
          courseId={courseId ?? ""}
          isOwner={isOwner}
        />
      )}

      <p className={styles.note}>
        提交与批改（契约第 9 节）尚未实现，因此任务页不提供上传与成绩入口。
      </p>
    </div>
  );
}
