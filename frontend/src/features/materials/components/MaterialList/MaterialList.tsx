/** 资料列表。统一处理 Loading / Empty / Error 三态。 */

import { Card } from "@/components/Card/Card";
import { EmptyState } from "@/components/EmptyState/EmptyState";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { Skeleton } from "@/components/Skeleton/Skeleton";
import type { MaterialVM } from "@/features/materials/model/types";

import { MaterialRow } from "../MaterialRow/MaterialRow";

import styles from "./MaterialList.module.css";

export type MaterialListProps = {
  materials: MaterialVM[];
  isPending: boolean;
  error: { message: string; requestId?: string } | null;
  onRetry: () => void;
  courseId: string;
  canManage: boolean;
  readOnly: boolean;
};

export function MaterialList({
  materials,
  isPending,
  error,
  onRetry,
  courseId,
  canManage,
  readOnly,
}: MaterialListProps) {
  if (isPending) {
    return (
      <Card>
        <div className={styles.skeletons} aria-busy="true">
          <Skeleton height={56} radius="12px" />
          <Skeleton height={56} radius="12px" />
          <Skeleton height={56} radius="12px" />
        </div>
      </Card>
    );
  }

  if (error) {
    return (
      <ErrorState
        title="资料加载失败"
        message={error.message}
        requestId={error.requestId}
        onRetry={onRetry}
      />
    );
  }

  if (materials.length === 0) {
    return (
      <EmptyState
        illustration="empty-learning"
        title="暂时没有课程资料"
        description={
          canManage
            ? "上传课件后，系统会异步解析出章节大纲和知识点。"
            : "教师上传课件并解析完成后，这里会出现课程大纲和知识点。"
        }
      />
    );
  }

  return (
    <Card>
      <ul className={styles.list}>
        {materials.map((material) => (
          <MaterialRow
            key={material.id}
            material={material}
            courseId={courseId}
            canManage={canManage}
            readOnly={readOnly}
          />
        ))}
      </ul>
    </Card>
  );
}
