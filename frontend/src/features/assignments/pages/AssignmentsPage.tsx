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
import { useNavigate, useParams } from "react-router-dom";

import { AssignmentForm } from "@/features/assignments/components/AssignmentForm/AssignmentForm";
import { AssignmentList } from "@/features/assignments/components/AssignmentList/AssignmentList";
import { useAssignments, useCreateAssignment } from "@/features/assignments/hooks/useAssignments";
import { suggestRubricFromFile, uploadAttachment } from "@/features/assignments/model/attachment";
import { useSetBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { useCourse } from "@/features/courses/hooks/useCourses";
import { toAppError } from "@/services/http";

import styles from "./AssignmentsPage.module.css";

export function AssignmentsPage() {
  const { courseId } = useParams<{ courseId: string }>();
  const navigate = useNavigate();
  const courseQuery = useCourse(courseId);
  const assignmentsQuery = useAssignments(courseId);
  const createAssignment = useCreateAssignment(courseId ?? "");
  const [creating, setCreating] = useState(false);
  const [uploadingAttachment, setUploadingAttachment] = useState(false);

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
            可在创建时选择作业文件。系统先创建草稿，再将所选文件上传为附件；
            创建后仍可在详情页添加或删除附件。
          </p>
          <AssignmentForm
            isPending={createAssignment.isPending || uploadingAttachment}
            error={createAssignment.error}
            onSuggestRubric={async (file, description, totalScore) =>
              (await suggestRubricFromFile(courseId ?? "", file, description, totalScore)).rubric_items
                .map((item) => ({ ...item, max_score: Number(item.max_score) }))
            }
            onSubmitCreate={(body, file) => {
              void (async () => {
                let created;
                try {
                  created = await createAssignment.mutateAsync(body);
                } catch {
                  return;
                }
                let attachmentUploadError = "";
                if (file) {
                  setUploadingAttachment(true);
                  try {
                    await uploadAttachment({ assignmentId: created.id, file });
                  } catch (cause) {
                    attachmentUploadError = `任务已创建，但附件上传失败：${cause instanceof Error ? cause.message : toAppError(cause).message}。请在详情页重试。`;
                  } finally {
                    setUploadingAttachment(false);
                  }
                }
                setCreating(false);
                navigate(`/courses/${courseId}/assignments/${created.id}`, {
                  state: attachmentUploadError ? { attachmentUploadError } : undefined,
                });
              })();
            }}
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

    </div>
  );
}
