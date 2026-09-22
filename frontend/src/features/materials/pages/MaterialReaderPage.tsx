/**
 * 资料阅读页。
 *
 * 契约只提供解析产物（章节 + 知识点 + 原文摘录 + 来源定位），
 * 不提供资料正文与下载地址，因此这里展示的就是大纲本身，
 * 不做「伪造原文」或「伪造页码」的事。
 */

import { Link, useParams, useSearchParams } from "react-router-dom";

import { Button } from "@/components/Button/Button";
import { Card } from "@/components/Card/Card";
import { EmptyState } from "@/components/EmptyState/EmptyState";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { Skeleton, SkeletonLines } from "@/components/Skeleton/Skeleton";
import { AgentActionButton } from "@/features/buddy/components/AgentActionButton/AgentActionButton";
import { useSetBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { MaterialOutlineView } from "@/features/materials/components/MaterialOutlineView/MaterialOutlineView";
import { MaterialStatusBadge } from "@/features/materials/components/MaterialStatusBadge/MaterialStatusBadge";
import { useMaterial, useMaterialOutline } from "@/features/materials/hooks/useMaterials";
import { toAppError } from "@/services/http";

import styles from "./MaterialReaderPage.module.css";

export function MaterialReaderPage() {
  const { courseId, materialId } = useParams<{ courseId: string; materialId: string }>();
  const [searchParams] = useSearchParams();
  const activeSectionId = searchParams.get("section") ?? undefined;

  const materialQuery = useMaterial(materialId);
  const outlineQuery = useMaterialOutline(materialId);

  useSetBuddyContext({
    courseId,
    entityType: "material",
    entityId: materialId,
    sectionId: activeSectionId,
    route: "",
  });

  const backTo = `/courses/${courseId}/materials`;

  if (materialQuery.isPending) {
    return (
      <div className={styles.page} aria-busy="true">
        <Skeleton height={26} width="34%" />
        <div className={styles.gap} />
        <Card>
          <SkeletonLines lines={3} />
        </Card>
      </div>
    );
  }

  if (materialQuery.isError) {
    const error = toAppError(materialQuery.error);
    return (
      <div className={styles.page}>
        <ErrorState
          title="资料加载失败"
          message={error.message}
          requestId={error.requestId}
          onRetry={() => void materialQuery.refetch()}
        />
      </div>
    );
  }

  const material = materialQuery.data;

  return (
    <div className={styles.page}>
      <div className={styles.breadcrumb}>
        <Link to={backTo} className={styles.back}>
          课程资料
        </Link>
        <span aria-hidden="true">›</span>
      </div>

      <header className={styles.header}>
        <div className={styles.headMain}>
          <h1 className={styles.title} title={material.filename}>
            {material.filename}
          </h1>
          <div className={styles.meta}>
            <MaterialStatusBadge material={material} />
            <span>{material.typeLabel}</span>
            <span>{material.sizeLabel}</span>
            <span>上传于 {material.createdAtLabel}</span>
          </div>
        </div>

        {material.isReady ? (
          <AgentActionButton
            label="总结这份资料"
            prompt={`请总结《${material.filename}》的主要内容与知识点。`}
            // 文档 6.9：这个按钮应当创建 SUMMARIZE_CONTEXT + MATERIAL 的 Run，
            // 而不是把意图写进文本让模型猜
            agentAction="SUMMARIZE_CONTEXT"
          />
        ) : null}
      </header>

      {!material.isReady ? (
        <MaterialNotReady
          status={material.status}
          statusLabel={material.statusLabel}
          errorMessage={material.errorMessage}
          backTo={backTo}
        />
      ) : outlineQuery.isPending ? (
        <Card>
          <div aria-busy="true">
            <SkeletonLines lines={6} />
          </div>
        </Card>
      ) : outlineQuery.isError ? (
        <OutlineFallback error={outlineQuery.error} onRetry={() => void outlineQuery.refetch()} />
      ) : (outlineQuery.data?.sections.length ?? 0) === 0 ? (
        <EmptyState
          title="这份资料没有解析出章节"
          description="解析成功但没有提取到章节内容，可能是文件本身没有可提取的文本（例如扫描版 PDF）。"
        />
      ) : (
        <MaterialOutlineView outline={outlineQuery.data!} activeSectionId={activeSectionId} />
      )}
    </div>
  );
}

/* --------------------------- 资料未就绪 --------------------------- */

type MaterialNotReadyProps = {
  status: string;
  statusLabel: string;
  errorMessage: string | null;
  backTo: string;
};

function MaterialNotReady({ status, statusLabel, errorMessage, backTo }: MaterialNotReadyProps) {
  // 契约 4.7：PROCESSING 是排队或处理中，不是失败
  const inFlight = status === "processing";

  return (
    <Card>
      <div className={styles.stateCard}>
        <p className={styles.stateTitle}>
          {inFlight ? "资料正在解析中" : `资料当前状态：${statusLabel}`}
        </p>
        <p className={styles.stateText}>
          {inFlight
            ? "解析由后台 Worker 执行，完成后这里会自动出现章节大纲与知识点。可以先离开这个页面。"
            : (errorMessage ?? "这份资料还没有可阅读的解析结果。")}
        </p>
        {!inFlight ? (
          <Link to={backTo} className={styles.stateLink}>
            回到资料列表
          </Link>
        ) : null}
      </div>
    </Card>
  );
}

/* --------------------------- 大纲错误分流 --------------------------- */

function OutlineFallback({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const appError = toAppError(error);

  // 契约 5.4：409 表示还没解析完，502 表示解析任务失败
  if (appError.code === "MATERIAL_NOT_READY") {
    return (
      <Card>
        <div className={styles.stateCard}>
          <p className={styles.stateTitle}>大纲还没有生成</p>
          <p className={styles.stateText}>
            资料仍在解析中（任务已受理）。稍后刷新即可，不需要重新上传。
          </p>
          <Button variant="secondary" size="sm" onClick={onRetry}>
            刷新
          </Button>
        </div>
      </Card>
    );
  }

  if (appError.code === "AI_JOB_FAILED") {
    const jobId = typeof appError.details.job_id === "string" ? appError.details.job_id : null;
    return (
      <Card>
        <div className={styles.stateCard}>
          <p className={styles.stateTitle}>资料解析失败</p>
          <p className={styles.stateText}>
            可以回到资料列表使用「重试解析」；重试会复用原来的任务，不会重复解析。
          </p>
          {jobId ? <p className={styles.jobId}>任务编号 {jobId}</p> : null}
        </div>
      </Card>
    );
  }

  return (
    <ErrorState
      title="大纲加载失败"
      message={appError.message}
      requestId={appError.requestId}
      onRetry={onRetry}
    />
  );
}
