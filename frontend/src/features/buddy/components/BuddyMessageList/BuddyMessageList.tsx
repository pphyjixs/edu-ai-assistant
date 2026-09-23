/**
 * 消息列表。负责 Loading / Empty / Error 三态与自动滚动。
 *
 * 除了消息本身，这里还要把**失败的 Run** 显示出来（评审文档「一、#11」）：
 * 失败的回答不会写进消息表，刷新之后用户只看得到自己的提问，
 * 完全不知道发生了什么。把 Run 的状态与失败阶段挂在对应提问下面，
 * 用户至少知道「上一次为什么没有回答、下一步能做什么」。
 */

import { useEffect, useRef } from "react";

import { EmptyState } from "@/components/EmptyState/EmptyState";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { Icon } from "@/components/Icon/Icon";
import { Skeleton } from "@/components/Skeleton/Skeleton";
import type { AgentRunDto } from "@/features/buddy/api";
import {
  useBuddyMessages,
  useRunByInputMessage,
} from "@/features/buddy/hooks/useBuddyThread";
import { toAppError } from "@/services/http";
import { failureStageHint, failureStageLabel } from "@/utils/failureStage";

import { BuddyMessage } from "../BuddyMessage/BuddyMessage";

import styles from "./BuddyMessageList.module.css";

export type BuddyMessageListProps = {
  sessionId: string | undefined;
  /** 正在等待助手回复 */
  isSending: boolean;
};

export function BuddyMessageList({ sessionId, isSending }: BuddyMessageListProps) {
  const { data, isPending, isError, error, refetch } = useBuddyMessages(sessionId);
  const runByInputMessage = useRunByInputMessage(sessionId);
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
      {data?.map((message) => {
        // Run 挂在**触发它的那条用户消息**下面
        const run = runByInputMessage.get(message.id);
        return (
          <div key={message.id}>
            <BuddyMessage message={message} />
            {run ? <BuddyRunNotice run={run} /> : null}
          </div>
        );
      })}

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

/**
 * 一次 Run 的状态提示。
 *
 * 只在**用户需要知道的结果**上出现：失败或取消时说明原因与下一步；
 * 正在执行时说明进度；成功时不重复提示（消息本身就是结果）。
 */
function BuddyRunNotice({ run }: { run: AgentRunDto }) {
  if (run.status === "SUCCEEDED" || run.status === "PENDING") return null;

  if (run.status === "RUNNING") {
    return (
      <p className={styles.notice} role="status">
        <Icon name="spark" size={13} />
        <span>正在生成回答…</span>
      </p>
    );
  }

  if (run.status === "CANCELLED") {
    return (
      <p className={styles.notice}>
        <span>这次提问已取消，没有生成回答。</span>
      </p>
    );
  }

  // FAILED：给出「哪一步失败 + 能做什么」，而不是一句笼统的错误
  return (
    <p className={`${styles.notice} ${styles.noticeError}`} role="alert">
      <strong>{failureStageLabel(run.failure_stage)}</strong>
      {run.error ? <span>：{run.error}</span> : null}
      <span className={styles.noticeHint}>{failureStageHint(run.failure_stage)}</span>
    </p>
  );
}
