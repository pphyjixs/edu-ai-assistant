/**
 * 错误状态。只展示后端返回的安全文案（AppError.message），
 * 绝不把异常堆栈或原始响应体丢给用户（见 docs/deployment-vercel.md 第 7 节）。
 */

import { Button } from "@/components/Button/Button";
import { cn } from "@/components/utils";

import styles from "./ErrorState.module.css";

export type ErrorStateProps = {
  title?: string;
  message: string;
  /** request_id 便于报告问题时定位，但不作为用户主要信息 */
  requestId?: string;
  onRetry?: () => void;
  className?: string;
};

export function ErrorState({
  title = "加载失败",
  message,
  requestId,
  onRetry,
  className,
}: ErrorStateProps) {
  return (
    <div className={cn(styles.error, className)} role="alert">
      <p className={styles.title}>{title}</p>
      <p className={styles.message}>{message}</p>
      {requestId ? <p className={styles.requestId}>请求编号 {requestId}</p> : null}
      {onRetry ? (
        <div className={styles.action}>
          <Button variant="secondary" size="sm" onClick={onRetry}>
            重试
          </Button>
        </div>
      ) : null}
    </div>
  );
}
