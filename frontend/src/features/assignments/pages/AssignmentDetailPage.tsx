/**
 * 作业详情 Workspace。
 *
 * 内容顺序遵循 DEVELOPMENT_SPEC 第 14 节：Header → 作业要求 → 评分标准 → 我的提交。
 * 页面级 AI Action 直接调用 useAskBuddy，把预设问题送进右侧 Buddy，
 * 由 Buddy 自己从上下文里知道「这是哪门课的哪份作业」。
 */

import { useParams } from "react-router-dom";

import { Card } from "@/components/Card/Card";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { Icon } from "@/components/Icon/Icon";
import { Skeleton, SkeletonLines } from "@/components/Skeleton/Skeleton";
import { AgentActionButton } from "@/features/buddy/components/AgentActionButton/AgentActionButton";
import { useSetBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { assignmentActions } from "@/features/buddy/model/actions";
import { AssignmentHeader } from "@/features/assignments/components/AssignmentHeader/AssignmentHeader";
import { RubricList } from "@/features/assignments/components/RubricList/RubricList";
import { SubmissionStatus } from "@/features/assignments/components/SubmissionStatus/SubmissionStatus";
import { SubmissionUploader } from "@/features/assignments/components/SubmissionUploader/SubmissionUploader";
import { useAssignment, useMySubmission } from "@/features/assignments/hooks/useAssignments";
import { toAppError } from "@/services/http";

import styles from "./AssignmentDetailPage.module.css";

const CHECK_PROMPT = "请帮我检查这份实验报告是否覆盖了评分项要求。";

export function AssignmentDetailPage() {
  const { courseId, assignmentId } = useParams<{ courseId: string; assignmentId: string }>();

  const assignmentQuery = useAssignment(assignmentId);
  const submissionQuery = useMySubmission(assignmentId);

  // 声明「当前对象是谁」——上下文同步后 Buddy 才知道当前作业
  useSetBuddyContext({
    courseId,
    entityType: "assignment",
    entityId: assignmentId,
    route: "",
  });

  if (assignmentQuery.isPending) {
    return (
      <div className={styles.page} aria-busy="true">
        <Skeleton height={28} width="42%" />
        <div className={styles.skeletonGap} />
        <Card>
          <SkeletonLines lines={6} />
        </Card>
        <div className={styles.skeletonGap} />
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
  const submission = submissionQuery.data ?? null;
  const hasSubmission = submission !== null;

  return (
    <div className={styles.page}>
      <AssignmentHeader assignment={assignment} />

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
              <AgentActionButton key={action.label} label={action.label} prompt={action.prompt} />
            ))}
          </div>
        </div>

        <div className={styles.docBody}>
          <p className={styles.docText}>{assignment.description}</p>
        </div>
      </Card>

      <Card className={styles.card}>
        <h2 className={styles.sectionTitle}>评分标准</h2>
        <RubricList
          rubric={assignment.rubric}
          totalScore={assignment.totalScore}
          mismatch={assignment.rubricMismatch}
        />
      </Card>

      <Card className={styles.card}>
        <div className={styles.submissionHead}>
          <div>
            <h2 className={styles.sectionTitle}>我的提交</h2>
            <p className={styles.sectionHint}>
              上传 PDF / Word 实验报告。提交前的自查由 Buddy 完成，与教师的正式批改是两回事。
            </p>
          </div>
          {hasSubmission ? <AgentActionButton label="提交前检查" prompt={CHECK_PROMPT} /> : null}
        </div>

        <SubmissionStatus submission={submission} />

        <SubmissionUploader
          assignmentId={assignment.id}
          disabled={!assignment.canSubmit}
          disabledReason={
            assignment.remaining.expired
              ? "作业已过截止时间，且不允许补交。"
              : "作业当前不接受提交。"
          }
        />
      </Card>
    </div>
  );
}
