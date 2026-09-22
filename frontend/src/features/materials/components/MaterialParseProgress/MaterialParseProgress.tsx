/**
 * 解析任务进度。
 *
 * 契约 10 的轮询建议：前 30 秒每 2 秒、之后每 5 秒，终态立即停止
 * （节奏由 services/jobs 的 useJobPolling 实现）。
 * 失败时展示后端返回的安全错误摘要与重试入口（契约 5.3）。
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { Button } from "@/components/Button/Button";
import { Pill } from "@/components/Pill/Pill";
import { useRetryParse } from "@/features/materials/hooks/useMaterials";
import { isJobInFlight, useJobPolling } from "@/services/jobs";
import { queryKeys } from "@/services/queryKeys";
import { toAppError } from "@/services/http";

import styles from "./MaterialParseProgress.module.css";

export type MaterialParseProgressProps = {
  jobId: string;
  courseId: string;
  materialId: string;
  filename: string;
};

export function MaterialParseProgress({
  jobId,
  courseId,
  materialId,
  filename,
}: MaterialParseProgressProps) {
  const queryClient = useQueryClient();
  const jobQuery = useJobPolling(jobId);
  const retryParse = useRetryParse(courseId, materialId);

  const job = jobQuery.data;
  const status = job?.status;

  // 解析成功后刷新资料列表与大纲
  useEffect(() => {
    if (status !== "SUCCEEDED") return;
    void queryClient.invalidateQueries({ queryKey: queryKeys.materials(courseId) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.material(materialId) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.materialOutline(materialId) });
  }, [status, queryClient, courseId, materialId]);

  if (jobQuery.isPending) {
    return (
      <div className={styles.panel} aria-busy="true">
        <p className={styles.title}>{filename}</p>
        <p className={styles.hint}>正在查询解析任务…</p>
      </div>
    );
  }

  if (jobQuery.isError) {
    return (
      <div className={styles.panel}>
        <p className={styles.title}>{filename}</p>
        <p className={styles.failed}>{toAppError(jobQuery.error).message}</p>
      </div>
    );
  }

  if (!job) return null;

  if (isJobInFlight(status)) {
    return (
      <div className={styles.panel}>
        <div className={styles.head}>
          <p className={styles.title}>{filename}</p>
          <Pill tone="info">{status === "PENDING" ? "排队中" : "解析中"}</Pill>
        </div>
        {/* progress 由后端给出，0 表示刚入队；不伪造进度 */}
        <div className={styles.track} role="progressbar" aria-valuenow={job.progress} aria-valuemin={0} aria-valuemax={100}>
          <span className={styles.bar} style={{ width: `${Math.max(job.progress, 3)}%` }} />
        </div>
        <p className={styles.hint}>
          解析完成后这里会自动更新，可以先去处理别的事情。
        </p>
      </div>
    );
  }

  if (status === "SUCCEEDED") {
    return (
      <div className={styles.panel}>
        <div className={styles.head}>
          <p className={styles.title}>{filename}</p>
          <Pill tone="success">解析完成</Pill>
        </div>
        <p className={styles.hint}>大纲与知识点已经可以查看了。</p>
      </div>
    );
  }

  if (status === "CANCELLED") {
    return (
      <div className={styles.panel}>
        <div className={styles.head}>
          <p className={styles.title}>{filename}</p>
          <Pill tone="neutral">已取消</Pill>
        </div>
        <p className={styles.hint}>课程归档后解析任务会被取消。</p>
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      <div className={styles.head}>
        <p className={styles.title}>{filename}</p>
        <Pill tone="danger">解析失败</Pill>
      </div>
      <p className={styles.failed}>{job.error ?? "解析没有成功完成。"}</p>
      <div className={styles.actions}>
        <Button
          variant="secondary"
          size="sm"
          disabled={retryParse.isPending}
          onClick={() => retryParse.mutate()}
        >
          {retryParse.isPending ? "正在重试…" : "重试解析"}
        </Button>
      </div>
      {retryParse.isError ? (
        <p className={styles.failed}>{toAppError(retryParse.error).message}</p>
      ) : null}
    </div>
  );
}
