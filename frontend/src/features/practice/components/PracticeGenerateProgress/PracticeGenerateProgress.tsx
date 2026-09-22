/**
 * 练习生成进度。
 *
 * 与资料解析不同，练习生成任务的重试走 `POST /jobs/{job_id}/retry`（契约 10.1）：
 * 复用原练习 ID 与 job ID，并清空旧题目。只有 `FAILED` 或租约过期的
 * `RUNNING` 可以重试，其余状态返回 409 `JOB_NOT_RETRYABLE`。
 */

import { Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { Button } from "@/components/Button/Button";
import { Pill } from "@/components/Pill/Pill";
import { isJobInFlight, useJobPolling, useRetryJob } from "@/services/jobs";
import { queryKeys } from "@/services/queryKeys";
import { toAppError } from "@/services/http";

import styles from "./PracticeGenerateProgress.module.css";

export type PracticeGenerateProgressProps = {
  jobId: string;
  courseId: string;
  setId: string;
};

export function PracticeGenerateProgress({
  jobId,
  courseId,
  setId,
}: PracticeGenerateProgressProps) {
  const queryClient = useQueryClient();
  const jobQuery = useJobPolling(jobId);
  const retryJob = useRetryJob();

  const status = jobQuery.data?.status;

  useEffect(() => {
    if (status !== "SUCCEEDED") return;
    void queryClient.invalidateQueries({ queryKey: queryKeys.practiceSet(setId) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.practiceSets(courseId) });
  }, [status, queryClient, setId, courseId]);

  if (jobQuery.isPending) {
    return (
      <div className={styles.panel} aria-busy="true">
        <p className={styles.hint}>正在查询生成任务…</p>
      </div>
    );
  }

  if (jobQuery.isError) {
    return (
      <div className={styles.panel}>
        <p className={styles.failed}>{toAppError(jobQuery.error).message}</p>
      </div>
    );
  }

  const job = jobQuery.data;
  if (!job) return null;

  const draftLink = (
    <Link className={styles.link} to={`/courses/${courseId}/learn/${setId}`}>
      查看这套练习
    </Link>
  );

  if (isJobInFlight(status)) {
    return (
      <div className={styles.panel}>
        <div className={styles.head}>
          <p className={styles.title}>正在生成练习</p>
          <Pill tone="info">{status === "PENDING" ? "排队中" : "生成中"}</Pill>
        </div>
        <div className={styles.track} role="progressbar" aria-valuenow={job.progress} aria-valuemin={0} aria-valuemax={100}>
          <span className={styles.bar} style={{ width: `${Math.max(job.progress, 3)}%` }} />
        </div>
        <p className={styles.hint}>生成完成后可以在这里预览并发布。可以先去做别的事。</p>
      </div>
    );
  }

  if (status === "SUCCEEDED") {
    return (
      <div className={styles.panel}>
        <div className={styles.head}>
          <p className={styles.title}>练习已生成</p>
          <Pill tone="success">待发布</Pill>
        </div>
        <p className={styles.hint}>题目已经就绪，确认没问题后发布给学生。</p>
        <div className={styles.actions}>{draftLink}</div>
      </div>
    );
  }

  if (status === "CANCELLED") {
    return (
      <div className={styles.panel}>
        <div className={styles.head}>
          <p className={styles.title}>生成已取消</p>
          <Pill tone="neutral">已取消</Pill>
        </div>
        <p className={styles.hint}>课程归档后生成中的练习会被取消。</p>
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      <div className={styles.head}>
        <p className={styles.title}>练习生成失败</p>
        <Pill tone="danger">失败</Pill>
      </div>
      <p className={styles.failed}>{job.error ?? "生成没有成功完成。"}</p>
      <div className={styles.actions}>
        <Button
          variant="secondary"
          size="sm"
          disabled={retryJob.isPending}
          onClick={() => retryJob.mutate(jobId)}
        >
          {retryJob.isPending ? "正在重试…" : "重试生成"}
        </Button>
        {draftLink}
      </div>
      {retryJob.isError ? (
        <p className={styles.failed}>{toAppError(retryJob.error).message}</p>
      ) : null}
    </div>
  );
}
