/**
 * 提交/批改相关请求的错误分流。
 *
 * 契约 9 的几类错误是**权限与状态的结论**，不是系统故障，界面上必须区别对待：
 *
 * - `403 ROLE_FORBIDDEN` / `403 COURSE_FORBIDDEN`：不是创建教师 / 不是本人 →
 *   说明原因，不给"重试"按钮（重试一万次也不会变）；
 * - `404 RESOURCE_NOT_FOUND`：不存在或不可见 → 同样不给重试；
 * - 其余（网络、5xx）才是"暂时读不到"，提供重试。
 *
 * 集中一处是因为列表、详情、批改三处都要这套分流。
 */

import { ErrorState } from "@/components/ErrorState/ErrorState";

import { toAppError } from "@/services/http";

import styles from "./GradingErrorNotice.module.css";

export type GradingErrorNoticeProps = {
  error: unknown;
  /** 业务性不可见时的说明文案 */
  forbiddenMessage?: string;
  notFoundMessage?: string;
  onRetry?: () => void;
  title?: string;
};

export function GradingErrorNotice({
  error,
  forbiddenMessage = "你没有查看这份提交的权限。",
  notFoundMessage = "这份提交不存在，或者你没有查看权限。",
  onRetry,
  title = "加载失败",
}: GradingErrorNoticeProps) {
  const appError = toAppError(error);

  if (appError.code === "ROLE_FORBIDDEN" || appError.code === "COURSE_FORBIDDEN") {
    return <p className={styles.notice}>{forbiddenMessage}</p>;
  }

  if (appError.code === "RESOURCE_NOT_FOUND") {
    return <p className={styles.notice}>{notFoundMessage}</p>;
  }

  return (
    <ErrorState
      title={title}
      message={appError.message}
      requestId={appError.requestId}
      onRetry={onRetry}
    />
  );
}
