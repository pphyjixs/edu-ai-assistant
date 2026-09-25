/**
 * Buddy 的消息读取与「创建 Agent Run + 轮询」。
 *
 * 为什么不再用同步发消息接口：
 *
 * - 同步接口会在 HTTP 请求内等待模型，模型耗时几分钟时既不适合 Serverless，
 *   也会让用户面对一个没有反馈的请求（``docs/local-development-agent-backend.md`` 3.3）；
 * - Run 接口先落库用户消息再返回 202，因此**用户消息立刻可见**，
 *   助手消息等 Worker 完成后由轮询带回，刷新页面也能看到历史。
 *
 * 轮询节奏沿用契约建议：前 30 秒每 2 秒，之后每 5 秒，到达终态立即停止。
 * 会话与消息属于服务端状态，全部交给 TanStack Query；
 * UI store 里只保留当前会话 id 及其所属课程。
 */

import {
  useIsMutating,
  useMutation,
  useMutationState,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";

import { HttpError, toAppError, type AppError } from "@/services/http";
import { queryKeys } from "@/services/queryKeys";

import {
  buddyApi,
  isTerminalRunStatus,
  type AgentRunActionDto,
  type AgentRunDto,
  type ChatSessionDto,
} from "../api";
import { buildRunRequest, newClientRequestId } from "../model/runs";
import { useBuddyStore } from "../store/buddyStore";

export function useBuddyMessages(sessionId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.messages(sessionId ?? "pending"),
    queryFn: ({ signal }) => buddyApi.listMessages(sessionId as string, signal),
    enabled: Boolean(sessionId),
    staleTime: 0,
  });
}

/** 提示词之外的 Run 列表条数上限（与后端默认一致） */
const RUN_LIST_LIMIT = 20;

/**
 * 会话内的 Run 列表。
 *
 * 刷新之后消息列表里只有"用户提问"，失败的回答连内容都没有；
 * 有了 Run 列表，界面才能把「上一次为什么没有回答」挂在对应的提问下面
 * （评审文档「一、#11」）。
 */
export function useSessionRuns(sessionId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.agentRuns(sessionId ?? "none"),
    queryFn: ({ signal }) => buddyApi.listRuns(sessionId as string, RUN_LIST_LIMIT, signal),
    enabled: Boolean(sessionId),
    staleTime: 0,
  });
}

/**
 * 把 Run 列表按「触发它的用户消息」索引，便于挂在消息下面展示。
 *
 * 同一个 input_message 理论上只有一个 Run，这里取最新的一条。
 */
export function useRunByInputMessage(
  sessionId: string | undefined,
): Map<string, AgentRunDto> {
  const query = useSessionRuns(sessionId);
  return useMemo(() => {
    const map = new Map<string, AgentRunDto>();
    for (const run of query.data?.items ?? []) {
      if (!map.has(run.input_message_id)) map.set(run.input_message_id, run);
    }
    return map;
  }, [query.data]);
}

/**
 * 发送 mutation 的全局标识。
 *
 * 面板和页面按钮是两个不同的 useMutation 调用点，
 * 靠 mutationKey + useIsMutating 才能共享「正在发送」状态。
 */
export const buddyRunMutationKey = ["buddy", "run"] as const;

const FAST_INTERVAL_MS = 2_000;
const SLOW_INTERVAL_MS = 5_000;
const FAST_WINDOW_MS = 30_000;

export type SendRunInput = {
  input: string;
  action: AgentRunActionDto;
  selectedSkillId?: string;
};

export type SendRunResult = {
  sessionId: string;
  runId: string;
};

/**
 * 创建一次 Agent Run。
 *
 * 需要会话时先建会话（契约 6 的会话按课程创建，换课后必须换会话，
 * 否则问题会发进旧课程），再把当前上下文与动作一起发给后端。
 */
export function useSendBuddyRun() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationKey: buddyRunMutationKey,

    mutationFn: async (payload: SendRunInput): Promise<SendRunResult> => {
      // 读取最新状态，避免闭包里拿到旧值
      const { buddyContext, activeChatSessionId, activeChatCourseId, openChatSession } =
        useBuddyStore.getState();

      const reusable =
        activeChatSessionId &&
        activeChatCourseId &&
        activeChatCourseId === buddyContext.courseId;

      let sessionId = reusable ? activeChatSessionId : undefined;

      if (!sessionId) {
        if (!buddyContext.courseId) {
          throw new HttpError({
            code: "VALIDATION_ERROR",
            message: "请先选择要提问的课程，再向 Buddy 提问。",
            status: 422,
          });
        }
        const session = await buddyApi.createSession(buddyContext.courseId);
        sessionId = session.id;
        openChatSession(sessionId, buddyContext.courseId);
      }

      const run = await buddyApi.createRun(
        sessionId,
        buildRunRequest(buddyContext, {
          input: payload.input,
          action: payload.action,
          selectedText: buddyContext.selectedText,
          selectedSkillId: payload.selectedSkillId,
        }),
      );
      return { sessionId, runId: run.id };
    },

    onSuccess: (result) => {
      // 用户消息在创建 Run 时已经落库，立刻刷新让它出现在对话里
      void queryClient.invalidateQueries({ queryKey: queryKeys.messages(result.sessionId) });
      void queryClient.invalidateQueries({ queryKey: ["recent-chat-sessions"] });
      // 让「当前进行中的 Run」与 Run 列表跟上，避免刷新后又退回旧状态
      void queryClient.invalidateQueries({
        queryKey: queryKeys.activeAgentRun(result.sessionId),
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.agentRuns(result.sessionId) });
    },
  });
}

/** 任意调用点触发的「正在提交」状态 */
export function useIsBuddySending(): boolean {
  return useIsMutating({ mutationKey: buddyRunMutationKey }) > 0;
}

/**
 * 轮询一个 Run 到终态。
 *
 * 成功后刷新消息列表（助手消息此时才写入），失败则把安全错误摘要交给界面。
 */
export function useAgentRun(runId: string | undefined, sessionId: string | undefined) {
  const queryClient = useQueryClient();
  const startedAt = useRef(Date.now());

  useEffect(() => {
    startedAt.current = Date.now();
  }, [runId]);

  const query = useQuery({
    queryKey: queryKeys.agentRun(runId ?? "none"),
    queryFn: ({ signal }) => buddyApi.getRun(runId as string, signal),
    enabled: Boolean(runId),
    staleTime: 0,
    retry: false,
    refetchInterval: (current) => {
      if (isTerminalRunStatus(current.state.data?.status)) return false;
      const elapsed = Date.now() - startedAt.current;
      return elapsed < FAST_WINDOW_MS ? FAST_INTERVAL_MS : SLOW_INTERVAL_MS;
    },
  });

  const status = query.data?.status;

  useEffect(() => {
    if (status !== "SUCCEEDED" || !sessionId) return;
    void queryClient.invalidateQueries({ queryKey: queryKeys.messages(sessionId) });
    void queryClient.invalidateQueries({ queryKey: ["recent-chat-sessions"] });
  }, [status, sessionId, queryClient]);

  // 到达终态后同步刷新列表与 active-run：失败原因要能立刻出现在消息下面
  useEffect(() => {
    if (!isTerminalRunStatus(status) || !sessionId) return;
    void queryClient.invalidateQueries({ queryKey: queryKeys.agentRuns(sessionId) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.activeAgentRun(sessionId) });
  }, [status, sessionId, queryClient]);

  return query;
}

/**
 * 最近一次提问的完整状态：会话、Run、进度与错误。
 *
 * 面板与页面按钮共用它，因此无论从哪触发，界面上的状态都是一致的。
 *
 * **刷新恢复**（评审文档「一、#11」）：本地 mutation 记录只在内存里，
 * 刷新后为空。所以这里再问一次服务端「这个会话还有没有没结束的 Run」，
 * 有就接着轮询——否则用户刷新后既看不到进度，也等不到那条回答。
 */
export function useActiveBuddyRun(): {
  sessionId: string | undefined;
  runId: string | undefined;
  run: ReturnType<typeof useAgentRun>["data"];
  isSubmitting: boolean;
  error: AppError | null;
} {
  const sessionIdFromStore = useBuddyStore((state) => state.activeChatSessionId);

  const states = useMutationState({
    filters: { mutationKey: buddyRunMutationKey },
    select: (mutation) => ({
      status: mutation.state.status,
      data: mutation.state.data as SendRunResult | undefined,
      error: mutation.state.error,
      submittedAt: mutation.state.submittedAt,
    }),
  });

  const latest = [...states].sort((a, b) => b.submittedAt - a.submittedAt)[0];

  // 服务端的「进行中 Run」：页面刚打开（刷新 / 换会话）时靠它恢复
  const activeQuery = useQuery({
    queryKey: queryKeys.activeAgentRun(sessionIdFromStore ?? "none"),
    queryFn: ({ signal }) => buddyApi.getActiveRun(sessionIdFromStore as string, signal),
    enabled: Boolean(sessionIdFromStore),
    staleTime: 0,
    retry: false,
  });

  // 本地这一次只在"它确实属于当前会话"时才算数，避免换课后串台
  const localSessionId =
    latest?.data?.sessionId && latest.data.sessionId === sessionIdFromStore
      ? latest.data.sessionId
      : undefined;
  const localRunId = localSessionId ? latest?.data?.runId : undefined;

  const runId = localRunId ?? activeQuery.data?.run?.id ?? undefined;
  const runQuery = useAgentRun(runId, localSessionId ?? sessionIdFromStore);

  const submitError =
    latest?.status === "error" && latest.error ? toAppError(latest.error) : null;
  const runError = runQuery.data?.error ?? activeQuery.data?.run?.error ?? null;
  const runStatus = runQuery.data?.status ?? activeQuery.data?.run?.status;
  const error: AppError | null =
    submitError ??
    (runStatus === "FAILED"
      ? {
          code: "AI_JOB_FAILED",
          message: runError ?? "这次生成没有成功完成，可以重新提问。",
          details: {},
          retryable: true,
        }
      : null);

  return {
    sessionId: localSessionId ?? sessionIdFromStore,
    runId,
    run: runQuery.data ?? activeQuery.data?.run ?? undefined,
    isSubmitting: latest?.status === "pending",
    error,
  };
}

export type RecentChatSessionVM = {
  id: string;
  courseId: string;
  /** 契约里会话没有标题，只能用最近活动时间作为可读标识 */
  activityLabel: string;
};

function activityLabelOf(session: ChatSessionDto): string {
  const stamp = session.last_message_at ?? session.created_at;
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return "对话";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

/**
 * 侧栏的「最近对话」。
 *
 * 只使用契约 6 已有的 ``GET /courses/{course_id}/chat-sessions``，
 * 在各课程之间做一次前端合并——没有新增跨课程的聚合接口。
 */
export function useRecentChatSessions(courseIds: string[], limit = 3) {
  const results = useQueries({
    queries: courseIds.map((courseId) => ({
      queryKey: queryKeys.chatSessions(courseId),
      queryFn: () => buddyApi.listSessions(courseId),
    })),
  });

  const sessions: RecentChatSessionVM[] = [];
  results.forEach((result, index) => {
    const courseId = courseIds[index];
    (result.data?.items ?? []).forEach((session) => {
      sessions.push({
        id: session.id,
        courseId,
        activityLabel: activityLabelOf(session),
      });
    });
  });

  sessions.sort((a, b) => b.activityLabel.localeCompare(a.activityLabel));

  return {
    sessions: sessions.slice(0, limit),
    isPending: results.some((result) => result.isPending),
  };
}

export { newClientRequestId };
