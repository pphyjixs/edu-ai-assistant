/**
 * 消息列表。负责 Loading / Empty / Error 三态与自动滚动。
 *
 * 除了消息本身，这里还要把**失败的 Run** 显示出来（评审文档「一、#11」）：
 * 失败的回答不会写进消息表，刷新之后用户只看得到自己的提问，
 * 完全不知道发生了什么。把 Run 的状态与失败阶段挂在对应提问下面，
 * 用户至少知道「上一次为什么没有回答、下一步能做什么」。
 *
 * 自动滚动有两种模式（开发方案 4.5）：
 *
 * - ``stickToBottom=false``（停靠面板，默认）：新消息一律滚到底；
 * - ``stickToBottom=true``（中央会话页）：只有用户本来就接近底部时才自动跟随，
 *   用户正在翻旧消息时不会被强行拉回。
 */

import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";

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
import {
  failureStageHint,
  failureStageLabel,
  toolErrorLabel,
} from "@/utils/failureStage";

import { BuddyMessage } from "../BuddyMessage/BuddyMessage";

import styles from "./BuddyMessageList.module.css";

/** 「接近底部」的判定阈值（px）：留一点余量，避免像素级抖动导致跟随中断 */
const NEAR_BOTTOM_THRESHOLD_PX = 80;

export type BuddyMessageListProps = {
  sessionId: string | undefined;
  /** 正在等待助手回复 */
  isSending: boolean;
  /** 是否只在接近底部时自动滚动（中央会话页传 true） */
  stickToBottom?: boolean;
  /**
   * 承载面：中央形态去掉自带的水平内边距，由 BuddyThreadView 统一对齐；
   * 面板形态保持原有的内边距。
   */
  variant?: "center" | "panel";
};

export function BuddyMessageList({
  sessionId,
  isSending,
  stickToBottom = false,
  variant = "panel",
}: BuddyMessageListProps) {
  const { data, isPending, isError, error, refetch } = useBuddyMessages(sessionId);
  const runByInputMessage = useRunByInputMessage(sessionId);
  const bottomRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  // 用户当前是否「贴着底部」；默认 true，首次进入时应当停在最新消息
  const nearBottom = useRef(true);

  const messageCount = data?.length ?? 0;

  function handleScroll() {
    const node = containerRef.current;
    if (!node) return;
    const distance = node.scrollHeight - node.scrollTop - node.clientHeight;
    nearBottom.current = distance <= NEAR_BOTTOM_THRESHOLD_PX;
  }

  useEffect(() => {
    if (stickToBottom && !nearBottom.current) return;
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messageCount, isSending, stickToBottom]);

  const listClassName =
    variant === "center"
      ? `${styles.list} ${styles.listCenter}`
      : styles.list;

  if (sessionId && isPending) {
    return (
      <div className={listClassName} aria-busy="true">
        <div className={styles.loadingBubble}>
          <Skeleton height={66} radius="14px" />
        </div>
      </div>
    );
  }

  if (sessionId && isError) {
    const appError = toAppError(error);
    return (
      <div className={listClassName}>
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
      <div className={listClassName} ref={containerRef} onScroll={handleScroll}>
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
    <div className={listClassName} ref={containerRef} onScroll={handleScroll}>
      {data?.map((message) => {
        // Run 挂在**触发它的那条用户消息**下面
        const run = runByInputMessage.get(message.id);
        return (
          <div key={message.id}>
            <BuddyMessage message={message} />
            {run ? <BuddyRunNotice run={run} /> : null}
            {run ? <BuddyRunArtifacts run={run} /> : null}
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
        <span>{runProgressText(run)}</span>
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
  const toolMessage = lastToolError(run);
  return (
    <p className={`${styles.notice} ${styles.noticeError}`} role="alert">
      <strong>{failureStageLabel(run.failure_stage)}</strong>
      {run.error ? <span>：{run.error}</span> : null}
      {toolMessage ? <span className={styles.noticeHint}>{toolMessage}</span> : null}
      <span className={styles.noticeHint}>{failureStageHint(run.failure_stage)}</span>
    </p>
  );
}

/**
 * 本次 Run 产出的业务结果（例如创建好的练习）。
 *
 * 工具写操作的真实结果必须在对话里可见，否则用户会以为"只是被回答了一段话"；
 * 卡片指向领域页面（练习页），进度由那条真实流程负责（开发方案第 8 节）。
 */
function BuddyRunArtifacts({ run }: { run: AgentRunDto }) {
  const artifacts = run.artifacts ?? [];
  if (artifacts.length === 0) return null;

  return (
    <div className={styles.artifacts}>
      {artifacts.map((artifact) => (
        <Link
          key={`${artifact.kind}-${artifact.id}`}
          to={artifact.href}
          className={styles.artifact}
        >
          <Icon name="assignment" size={14} />
          <span className={styles.artifactText}>
            <strong>{artifactLabel(artifact.kind)}</strong>
            <span className={styles.artifactHint}>
              {artifact.status === "PENDING" || artifact.status === "RUNNING"
                ? "生成中，点击查看进度"
                : "点击进入查看"}
            </span>
          </span>
        </Link>
      ))}
    </div>
  );
}

function artifactLabel(kind: string): string {
  switch (kind) {
    case "PRACTICE_SET":
      return "已创建一套练习";
    default:
      return "已创建站内内容";
  }
}

/** 最后一个失败步骤的稳定错误码 → 人类文案；没有就返回 null */
function lastToolError(run: AgentRunDto): string | null {
  const steps = run.steps ?? [];
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const step = steps[index];
    if (step.status === "FAILED") {
      const label = toolErrorLabel(step.error_code);
      if (label) return label;
    }
  }
  return null;
}

/**
 * 运行中的进度文案：优先显示**最新一个 step** 的状态
 * （「正在检索课程资料」），没有 step 时退回通用文案。
 * 不展示模型内部推理，只展示受控的工具名映射（开发方案第 8 节）。
 */
function runProgressText(run: AgentRunDto): string {
  const steps = run.steps ?? [];
  const last = steps[steps.length - 1];
  if (last) {
    return toolLabel(last.name, last.status);
  }
  return "正在生成回答…";
}

/** 工具名 → 用户可见短文案（开发方案第 8 节的映射表） */
export const TOOL_LABELS: Record<string, string> = {
  search_course_knowledge: "正在检索课程资料",
  list_course_materials: "正在读取资料列表",
  list_course_assignments: "正在查看课程作业",
  get_assignment: "正在读取作业要求",
  generate_practice: "正在创建练习",
  load_skill: "正在加载任务工作流",
};

export function toolLabel(name: string | undefined | null, status?: string): string {
  if (!name) return "正在生成回答…";
  const base = TOOL_LABELS[name] ?? "正在处理";
  if (status === "FAILED") return `${base}（失败）`;
  return `${base}…`;
}
