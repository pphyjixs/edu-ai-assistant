/**
 * 资料列表中的一行。
 *
 * 学生只看到「查看大纲」；教师额外有「重试解析」与「删除」，
 * 且删除与重试只在后端允许的状态下出现（契约 5.2 / 5.3）。
 */

import { Link } from "react-router-dom";

import { Button } from "@/components/Button/Button";
import { useDeleteMaterial, useRetryParse } from "@/features/materials/hooks/useMaterials";
import type { MaterialVM } from "@/features/materials/model/types";
import { toAppError } from "@/services/http";

import { MaterialStatusBadge } from "../MaterialStatusBadge/MaterialStatusBadge";

import styles from "./MaterialRow.module.css";

export type MaterialRowProps = {
  material: MaterialVM;
  courseId: string;
  /** 当前用户是否为课程创建教师 */
  canManage: boolean;
  /** 归档课程只读 */
  readOnly: boolean;
};

export function MaterialRow({ material, courseId, canManage, readOnly }: MaterialRowProps) {
  const deleteMaterial = useDeleteMaterial(courseId);
  const retryParse = useRetryParse(courseId, material.id);

  const deleteError = deleteMaterial.isError ? toAppError(deleteMaterial.error) : null;
  const retryError = retryParse.isError ? toAppError(retryParse.error) : null;

  const detailPath = `/courses/${courseId}/materials/${material.id}`;

  return (
    <li className={styles.row}>
      <div className={styles.main}>
        <span className={styles.typeBadge} aria-hidden="true">
          {material.typeLabel}
        </span>

        <div className={styles.info}>
          <Link to={detailPath} className={styles.filename} title={material.filename}>
            {material.filename}
          </Link>
          <div className={styles.meta}>
            <MaterialStatusBadge material={material} />
            <span>{material.sizeLabel}</span>
            <span>上传于 {material.createdAtLabel}</span>
          </div>
          {material.errorMessage ? (
            <p className={styles.errorMessage}>{material.errorMessage}</p>
          ) : null}
          {retryError ? <p className={styles.errorMessage}>{retryError.message}</p> : null}
          {deleteError ? <p className={styles.errorMessage}>{deleteError.message}</p> : null}
        </div>
      </div>

      <div className={styles.actions}>
        <Link to={detailPath} className={styles.linkButton}>
          {material.isReady ? "查看大纲" : "查看状态"}
        </Link>

        {canManage && !readOnly && material.canRetry ? (
          <Button
            variant="ghost"
            size="sm"
            iconLeft="spark"
            disabled={retryParse.isPending}
            onClick={() => retryParse.mutate()}
          >
            {retryParse.isPending ? "重试中…" : "重试解析"}
          </Button>
        ) : null}

        {canManage && !readOnly ? (
          <Button
            variant="ghost"
            size="sm"
            disabled={deleteMaterial.isPending}
            onClick={() => deleteMaterial.mutate(material.id)}
            aria-label={`删除 ${material.filename}`}
          >
            {deleteMaterial.isPending ? "删除中…" : "删除"}
          </Button>
        ) : null}
      </div>
    </li>
  );
}
