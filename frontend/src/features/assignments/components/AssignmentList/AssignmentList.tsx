/** 作业列表。统一处理 Loading / Empty / Error 三态。 */

import { Card } from "@/components/Card/Card";
import { EmptyState } from "@/components/EmptyState/EmptyState";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { Skeleton } from "@/components/Skeleton/Skeleton";
import type { AssignmentVM } from "@/features/assignments/model/types";

import { AssignmentRow } from "../AssignmentRow/AssignmentRow";

import styles from "./AssignmentList.module.css";

export type AssignmentListProps = {
  assignments: AssignmentVM[];
  isPending: boolean;
  error: { message: string; requestId?: string } | null;
  onRetry: () => void;
  courseId: string;
  isOwner: boolean;
};

export function AssignmentList({
  assignments,
  isPending,
  error,
  onRetry,
  courseId,
  isOwner,
}: AssignmentListProps) {
  if (isPending) {
    return (
      <Card>
        <div className={styles.skeletons} aria-busy="true">
          <Skeleton height={52} radius="12px" />
          <Skeleton height={52} radius="12px" />
        </div>
      </Card>
    );
  }

  if (error) {
    return (
      <ErrorState
        title="作业加载失败"
        message={error.message}
        requestId={error.requestId}
        onRetry={onRetry}
      />
    );
  }

  if (assignments.length === 0) {
    return (
      <EmptyState
        illustration="empty-learning"
        title="还没有实验任务"
        description={
          isOwner
            ? "创建任务时可以配置评分标准；评分项分值合计必须等于总分。"
            : "教师发布实验任务后，会出现在这里。"
        }
      />
    );
  }

  return (
    <Card>
      <ul className={styles.list}>
        {assignments.map((assignment) => (
          <AssignmentRow key={assignment.id} assignment={assignment} courseId={courseId} />
        ))}
      </ul>
    </Card>
  );
}
