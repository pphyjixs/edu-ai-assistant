/**
 * 消息列表。负责 Loading / Empty / Error 三态与自动滚动。
 */

import { useEffect, useRef } from "react";

import { EmptyState } from "@/components/EmptyState/EmptyState";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { Skeleton } from "@/components/Skeleton/Skeleton";
import { useBuddyMessages } from "@/features/buddy/hooks/useBuddyThread";
import { toAppError } from "@/services/http";

import { BuddyMessage } from "../BuddyMessage/BuddyMessage";

import styles from "./BuddyMessageList.module.css";

export type BuddyMessageListProps = {
  sessionId: string | undefined;
  /** 正在等待助手回复 */
  isSending: boolean;
};

export function BuddyMessageList({ sessionId, isSending }: BuddyMessageListProps) {
  const { data, isPending, isError, error, refetch } = useBuddyMessages(sessionId);
  const bottomRef = useRef<HTMLDivElement>(null);

  const messageCount = data?.length ?? 0;

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messageCount, isSending]);

  if (sessionId && isPending) {
    return (
      <div className={styles.list} aria-busy="true">
        <div className={styles.loadingBubble}>
          <Skeleton height={66} radius="14px" />
        </div>
      </div>
    );
  }

  if (sessionId && isError) {
    const appError = toAppError(error);
    return (
      <div className={styles.list}>
        <ErrorState
          title="会话加载失败"
          message={appError.message}
          requestId={appError.requestId}
          onRetry={() => void refetch()}
        />
      </div>
    );
  }

  if (messageCount === 0) {
    return (
      <div className={styles.list}>
        <EmptyState
          illustration="empty-learning"
          title="开始和 Buddy 对话"
          description="我已经带上当前课程与学习对象的上下文，你可以直接问，不用重新交代背景。"
        />
        <div ref={bottomRef} />
      </div>
    );
  }

  return (
    <div className={styles.list}>
      {data?.map((message) => (
        <BuddyMessage key={message.id} message={message} />
      ))}

      {isSending ? (
        <div className={styles.sending} role="status">
          <span className={styles.sendingDot} />
          <span className={styles.sendingDot} />
          <span className={styles.sendingDot} />
          <span className={styles.sendingText}>Buddy 正在整理课程资料…</span>
        </div>
      ) : null}

      <div ref={bottomRef} />
    </div>
  );
}
