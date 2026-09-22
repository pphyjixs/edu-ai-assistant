/**
 * 异步任务查询与轮询。
 *
 * 契约依据：docs/api-contract.md 第 10 节。
 * - ``GET /jobs/{job_id}`` 可见性等同其关联资源：MATERIAL_PARSE 按资料可见性，
 *   PRACTICE_GENERATE 按练习可见性（学生只在该练习 PUBLISHED 时可见）。
 * - 轮询节奏：前 30 秒每 2 秒一次，之后每 5 秒一次；终态立即停止；
 *   页面离开（组件卸载）时由 React Query 自动停止。
 * - ``PROCESSING`` + ``PENDING`` 是排队中，不是失败。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { http } from "@/services/http";
import { queryKeys } from "@/services/queryKeys";
import type { Schemas } from "@/types/api";

export type JobStatusDto = Schemas["JobStatus"];
export type JobStatusValue = Schemas["JobStatusValue"];

const TERMINAL: readonly JobStatusValue[] = ["SUCCEEDED", "FAILED", "CANCELLED"];

export function isTerminalJobStatus(status: JobStatusValue | undefined): boolean {
  return status !== undefined && TERMINAL.includes(status);
}

/** 任务是否处于「等待或执行中」——UI 应展示为排队中/处理中，而不是失败 */
export function isJobInFlight(status: JobStatusValue | undefined): boolean {
  return status === "PENDING" || status === "RUNNING";
}

const FAST_INTERVAL_MS = 2_000;
const SLOW_INTERVAL_MS = 5_000;
const FAST_WINDOW_MS = 30_000;

export const jobsApi = {
  /** 契约 10：GET /jobs/{job_id} */
  get(jobId: string, signal?: AbortSignal): Promise<JobStatusDto> {
    return http.get<JobStatusDto>(`/jobs/${jobId}`, { signal });
  },

  /**
   * 契约 10.1：POST /jobs/{job_id}/retry
   * 仅课程创建教师可调用；只对 FAILED（或租约过期的 RUNNING）有效，
   * 其余状态返回 409 JOB_NOT_RETRYABLE。
   */
  retry(jobId: string): Promise<JobStatusDto> {
    return http.post<JobStatusDto>(`/jobs/${jobId}/retry`, {});
  },
};

/**
 * 轮询一个任务直到终态。
 *
 * ``jobId`` 为 undefined 时不发请求（例如表单尚未提交生成任务）。
 */
export function useJobPolling(jobId: string | undefined) {
  const startedAt = useRef(Date.now());

  useEffect(() => {
    startedAt.current = Date.now();
  }, [jobId]);

  return useQuery({
    queryKey: queryKeys.job(jobId ?? "none"),
    queryFn: ({ signal }) => jobsApi.get(jobId as string, signal),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (isTerminalJobStatus(status)) return false;
      return Date.now() - startedAt.current < FAST_WINDOW_MS ? FAST_INTERVAL_MS : SLOW_INTERVAL_MS;
    },
    staleTime: 0,
  });
}

export function useRetryJob() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (jobId: string) => jobsApi.retry(jobId),
    onSuccess: (job) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.job(job.id) });
    },
  });
}
