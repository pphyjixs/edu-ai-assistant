/**
 * 资料阅读页。
 *
 * 契约只提供解析产物（章节 + 知识点 + 原文摘录 + 来源定位），
 * 不提供资料正文与下载地址，因此这里展示的就是大纲本身，
 * 不做「伪造原文」或「伪造页码」的事。
 *
 * 上下文按**当前是否停在某一节**分流（评审文档「一、#3」）：
 * 有 `?section=` 时声明 `MATERIAL_SECTION` 并把 section_id 一起发出去，
 * 否则才声明整份 `MATERIAL`。旧实现固定声明 `material`，
 * 于是「总结本节」实际读的是整份资料。
 */

import { useCallback } from "react";
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
import {
  useMaterial,
  useMaterialOutline,
  useRetryParse,
} from "@/features/materials/hooks/useMaterials";
import { toAppError } from "@/services/http";

import styles from "./MaterialReaderPage.module.css";

export function MaterialReaderPage() {
  const { courseId, materialId } = useParams<{ courseId: string; materialId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeSectionId = searchParams.get("section") ?? undefined;

  const materialQuery = useMaterial(materialId);
  const outlineQuery = useMaterialOutline(materialId);
  const retry = useRetryParse(courseId ?? "", materialId ?? "");

  // 有活动章节就是「这一节」，否则是「整份资料」——两者注入的范围不同
  useSetBuddyContext({
    courseId,
    entityType: activeSectionId ? "material-section" : "material",
    entityId: materialId,
    sectionId: activeSectionId,
    route: "",
  });

  /**
   * 点目录选中某一节：写进 URL。
   *
   * 这是「总结本节」的前提——没有活动章节，页面就没有办法表达
   * "用户正在看哪一节"（评审文档「一、#3」）。
   */
  const selectSection = useCallback(
    (sectionId: string) => {
      setSearchParams({ section: sectionId }, { replace: true });
    },
    [setSearchParams],
  );

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
  const activeSectionTitle = activeSectionId
    ? outlineQuery.data?.sections.find((section) => section.id === activeSectionId)?.title
    : undefined;

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
          activeSectionId ? (
            <div className={styles.actions}>
              <AgentActionButton
                label="总结本节"
                prompt={`请总结「${activeSectionTitle ?? "这一节"}」这一节的主要内容。`}
                // 章节上下文必须带 section_id，后端才会只注入这一节及相邻章节
                agentAction="SUMMARIZE_CONTEXT"
              />
              <AgentActionButton
                label="总结整份资料"
                prompt={`请总结《${material.filename}》的主要内容与知识点。`}
                agentAction="SUMMARIZE_CONTEXT"
                contextPatch={{ entityType: "material", sectionId: undefined }}
              />
            </div>
          ) : (
            <AgentActionButton
              label="总结这份资料"
              prompt={`请总结《${material.filename}》的主要内容与知识点。`}
              // 文档 6.9：这个按钮应当创建 SUMMARIZE_CONTEXT + MATERIAL 的 Run，
              // 而不是把意图写进文本让模型猜
              agentAction="SUMMARIZE_CONTEXT"
            />
          )
        ) : null}
      </header>

      {!material.isReady ? (
        <MaterialNotReady
          material={material}
          isRetrying={retry.isPending}
          retryError={retry.isError ? toAppError(retry.error).message : null}
          onRetry={() => retry.mutate()}
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
        <MaterialOutlineView
          outline={outlineQuery.data!}
          activeSectionId={activeSectionId}
          onSelectSection={selectSection}
        />
      )}
    </div>
  );
}

/* --------------------------- 资料未就绪 --------------------------- */

type MaterialNotReadyProps = {
  material: ReturnType<typeof useMaterial>["data"] & object;
  isRetrying: boolean;
  retryError: string | null;
  onRetry: () => void;
  backTo: string;
};

function MaterialNotReady({
  material,
  isRetrying,
  retryError,
  onRetry,
  backTo,
}: MaterialNotReadyProps) {
  // 契约 4.7：PROCESSING 是排队或处理中，不是失败
  const inFlight = material.status === "processing";

  return (
    <Card>
      <div className={styles.stateCard}>
        <p className={styles.stateTitle}>
          {inFlight
            ? "资料正在解析中"
            : material.failureStageLabel
              ? `资料解析失败 · ${material.failureStageLabel}`
              : `资料当前状态：${material.statusLabel}`}
        </p>
        <p className={styles.stateText}>
          {inFlight
            ? "解析由后台 Worker 执行，完成后这里会自动出现章节大纲与知识点。可以先离开这个页面。"
            : (material.errorMessage ?? "这份资料还没有可阅读的解析结果。")}
        </p>

        {/* 失败时给出下一步能做什么，而不是只报告失败 */}
        {material.canRetry ? (
          <>
            {material.failureHint ? (
              <p className={styles.stateHint}>{material.failureHint}</p>
            ) : null}
            {retryError ? (
              <p className={styles.jobId} role="alert">
                {retryError}
              </p>
            ) : null}
            <div className={styles.stateActions}>
              <Button
                variant="primary"
                size="sm"
                onClick={onRetry}
                disabled={isRetrying}
              >
                {isRetrying ? "正在提交重试…" : "重试解析"}
              </Button>
              <Link to={backTo} className={styles.stateLink}>
                回到资料列表
              </Link>
            </div>
          </>
        ) : (
          <Link to={backTo} className={styles.stateLink}>
            回到资料列表
          </Link>
        )}
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
