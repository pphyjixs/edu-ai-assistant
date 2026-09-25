/**
 * 首页中央会话的数据逻辑（开发方案 4.1 / 4.4）。
 *
 * 两条链路：
 *
 * 1. :func:`useStartHomeChat` —— 首页 Omnibox 发送：创建/复用会话 → 创建 Run →
 *    导航到 ``/chats/:sessionId``。它**不打开右侧面板**，失败时保留输入内容
 *    并把可读错误交给页面展示。
 * 2. :func:`useHomeChatSession` —— 中央会话页刷新恢复：只有 ``sessionId`` 时
 *    调 ``GET /chat-sessions/{id}`` 拿回所属课程，并写回 store。
 *    **以服务端的 course_id 为准**，不相信 URL 或 sessionStorage 里的课程 ID。
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { queryKeys } from "@/services/queryKeys";
import { toAppError, type AppError } from "@/services/http";

import type { ChatSessionDto } from "../api";
import { buddyApi } from "../api";
import { useBuddyStore } from "../store/buddyStore";

import { useSendBuddyRun } from "./useBuddyThread";

/** 会话详情：中央会话页刷新时据此恢复课程上下文 */
export function useChatSession(sessionId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.chatSession(sessionId ?? "none"),
    queryFn: ({ signal }) => buddyApi.getSession(sessionId as string, signal),
    enabled: Boolean(sessionId),
    staleTime: 30_000,
  });
}

/**
 * 加载会话并把归属写回 store。
 *
 * 用响应里的 ``course_id`` 恢复 ``activeChatCourseId`` 与 Buddy 上下文——
 * 浏览器的历史记录、手改 URL、过期的 sessionStorage 都可能带着错误的课程 ID。
 */
export function useHomeChatSession(sessionId: string | undefined) {
  const openChatSession = useBuddyStore((state) => state.openChatSession);
  const query = useChatSession(sessionId);

  useEffect(() => {
    const session = query.data;
    if (!session) return;
    openChatSession(session.id, session.course_id);
  }, [query.data, openChatSession]);

  return query;
}

export type StartHomeChatResult = {
  /** 发送首页第一条消息；失败时抛出，调用方保留草稿 */
  start: (prompt: string) => Promise<string | undefined>;
  /** 最近一次发送失败的可读错误（展示在 Omnibox 下方） */
  error: AppError | null;
  clearError: () => void;
  isSubmitting: boolean;
};

/**
 * 首页发送：创建 Run 后进入中央会话页。
 *
 * 顺序固定为「校验课程与非空输入 → 创建/复用会话 → 创建 Run →
 * 导航到 ``/chats/{sessionId}`` → 刷新用户消息与 Run 状态」。
 * 课程与空输入的校验在 :func:`useSendBuddyRun` 内完成（缺课程直接抛
 * ``VALIDATION_ERROR``），错误文案由页面显示。
 */
export function useStartHomeChat(): StartHomeChatResult {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const send = useSendBuddyRun();
  const [error, setError] = useState<AppError | null>(null);

  const start = useCallback(
    async (prompt: string): Promise<string | undefined> => {
      setError(null);
      try {
        const result = await send.mutateAsync({ input: prompt, action: "ASK" });
        // Run 创建成功后用户消息已落库，立刻带过去让中央会话显示
        void queryClient.invalidateQueries({
          queryKey: queryKeys.messages(result.sessionId),
        });
        navigate(`/chats/${result.sessionId}`);
        return result.sessionId;
      } catch (cause) {
        const appError = toAppError(cause);
        setError(appError);
        throw appError;
      }
    },
    [navigate, queryClient, send],
  );

  const clearError = useCallback(() => setError(null), []);

  return { start, error, clearError, isSubmitting: send.isPending };
}

export type { ChatSessionDto };
