/**
 * 作业详情 Workspace。
 *
 * 内容顺序遵循 DEVELOPMENT_SPEC 第 14 节：Header → 作业要求 → 评分标准 → 我的提交。
 * 页面级 AI Action 把预设问题送进右侧 Buddy；教师额外拥有编辑/发布/关闭。
 *
 * 契约 8.1：状态只由发布/关闭接口推进，`due_at` 过期不会把任务变成 CLOSED，
 * 因此「已截止」与「已关闭」在这里是两件事，分别由时间与状态表达。
 */

import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { Button } from "@/components/Button/Button";
import { Card } from "@/components/Card/Card";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { Icon } from "@/components/Icon/Icon";
import { Skeleton, SkeletonLines } from "@/components/Skeleton/Skeleton";
import { AssignmentForm } from "@/features/assignments/components/AssignmentForm/AssignmentForm";
import { AssignmentHeader } from "@/features/assignments/components/AssignmentHeader/AssignmentHeader";
import { RubricList } from "@/features/assignments/components/RubricList/RubricList";
import { SubmissionPanel } from "@/features/assignments/components/SubmissionPanel/SubmissionPanel";
import {
  useAssignment,
  useCloseAssignment,
  usePublishAssignment,
  useUpdateAssignment,
} from "@/features/assignments/hooks/useAssignments";
import { AgentActionButton } from "@/features/buddy/components/AgentActionButton/AgentActionButton";
import { useSetBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { assignmentActions } from "@/features/buddy/model/actions";
import { useCourse } from "@/features/courses/hooks/useCourses";
import { toAppError } from "@/services/http";

import styles from "./AssignmentDetailPage.module.css";

export function AssignmentDetailPage() {
  const { courseId, assignmentId } = useParams<{ courseId: string; assignmentId: string }>();

  const assignmentQuery = useAssignment(assignmentId);
  const courseQuery = useCourse(courseId);
  const [editing, setEditing] = useState(false);

  const updateAssignment = useUpdateAssignment(courseId ?? "", assignmentId ?? "");
  const publishAssignment = usePublishAssignment(courseId ?? "", assignmentId ?? "");
  const closeAssignment = useCloseAssignment(courseId ?? "", assignmentId ?? "");

  useSetBuddyContext({
    courseId,
    entityType: "assignment",
    entityId: assignmentId,
    route: "",
  });

  const isOwner = Boolean(courseQuery.data?.isOwner);
  const readOnly = courseQuery.data?.status === "archived";

  if (assignmentQuery.isPending) {
    return (
      <div className={styles.page} aria-busy="true">
        <Skeleton height={28} width="42%" />
        <div className={styles.skeletonGap} />
        <Card>
          <SkeletonLines lines={6} />
        </Card>
      </div>
    );
  }

  if (assignmentQuery.isError) {
    const error = toAppError(assignmentQuery.error);

    // 学生读草稿会得到 404（契约 8.1），文案上不要暗示「权限不足」
    if (error.code === "RESOURCE_NOT_FOUND") {
      return (
        <div className={styles.page}>
          <Card>
            <div className={styles.stateCard}>
              <p className={styles.stateTitle}>任务不存在或尚未发布</p>
              <p className={styles.stateText}>
                这个任务可能还是草稿，或者链接已经失效。
              </p>
              <Link className={styles.stateLink} to={`/courses/${courseId}/assignments`}>
                回到任务列表
              </Link>
            </div>
          </Card>
        </div>
      );
    }

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
  const actionError =
    (updateAssignment.isError && toAppError(updateAssignment.error)) ||
    (publishAssignment.isError && toAppError(publishAssignment.error)) ||
    (closeAssignment.isError && toAppError(closeAssignment.error)) ||
    null;

  const teacherActions =
    isOwner && !readOnly ? (
      <>
        {assignment.status === "draft" ? (
          <Button
            variant="primary"
            size="sm"
            disabled={publishAssignment.isPending}
            onClick={() => publishAssignment.mutate()}
          >
            {publishAssignment.isPending ? "发布中…" : "发布给学生"}
          </Button>
        ) : null}
        {assignment.status === "published" ? (
          <Button
            variant="secondary"
            size="sm"
            disabled={closeAssignment.isPending}
            onClick={() => closeAssignment.mutate()}
          >
            {closeAssignment.isPending ? "关闭中…" : "关闭提交"}
          </Button>
        ) : null}
        {assignment.canEdit && !editing ? (
          <Button variant="ghost" size="sm" onClick={() => setEditing(true)}>
            编辑任务
          </Button>
        ) : null}
      </>
    ) : null;

  return (
    <div className={styles.page}>
      <div className={styles.breadcrumb}>
        <Link to={`/courses/${courseId}/assignments`} className={styles.back}>
          实验任务
        </Link>
        <span aria-hidden="true">›</span>
      </div>

      <AssignmentHeader assignment={assignment} actions={teacherActions} />

      {actionError ? (
        <p className={styles.error} role="alert">
          {actionError.message}
        </p>
      ) : null}

      {editing && isOwner ? (
        <Card className={styles.card}>
          <h2 className={styles.sectionTitle}>编辑任务</h2>
          <p className={styles.sectionHint}>
            修改评分项或总分且内容确有变化时，后端会生成新的评分规则版本；
            只改标题、说明或截止时间不会。
          </p>
          <AssignmentForm
            assignment={assignment}
            isPending={updateAssignment.isPending}
            error={updateAssignment.error}
            onSubmitCreate={() => undefined}
            onSubmitUpdate={(body) =>
              updateAssignment.mutate(body, { onSuccess: () => setEditing(false) })
            }
            onCancel={() => setEditing(false)}
          />
        </Card>
      ) : null}

      <Card className={styles.card}>
        <div className={styles.toolbar}>
          <div className={styles.toolbarTitle}>
            <span className={styles.docIcon} aria-hidden="true">
              <Icon name="file" size={16} />
            </span>
            <span className={styles.toolbarText}>
              <strong>作业要求</strong>
              <span>{assignment.title}</span>
            </span>
          </div>

          <div className={styles.actions}>
            {assignmentActions.map((action) => (
              <AgentActionButton
                key={action.label}
                label={action.label}
                prompt={action.prompt}
                agentAction={action.agentAction}
              />
            ))}
          </div>
        </div>

        <div className={styles.docBody}>
          <p className={styles.docText}>
            {assignment.description || "这份任务还没有填写说明。"}
          </p>
        </div>
      </Card>

      <Card className={styles.card}>
        <h2 className={styles.sectionTitle}>评分标准</h2>
        <p className={styles.sectionHint}>
          当前评分规则版本 v{assignment.rubricVersion}，合计 {assignment.rubricScoreSum} 分。
        </p>
        <RubricList
          rubric={assignment.rubric}
          totalScore={assignment.totalScore}
          mismatch={assignment.rubricMismatch}
        />
      </Card>

      <div className={styles.card}>
        <SubmissionPanel assignment={assignment} isOwner={isOwner} />
      </div>
    </div>
  );
}
