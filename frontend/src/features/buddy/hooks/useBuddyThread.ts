/**
 * 会话消息的读取与发送。
 *
 * 会话与消息属于服务端状态，因此走 TanStack Query；
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

import { HttpError, toAppError, type AppError } from "@/services/http";
import { queryKeys } from "@/services/queryKeys";

import { buddyApi, buildSendRequest, type ChatSessionDto } from "../api";
import { useBuddyStore } from "../store/buddyStore";

export function useBuddyMessages(sessionId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.messages(sessionId ?? "pending"),
    queryFn: () => buddyApi.listMessages(sessionId as string),
    enabled: Boolean(sessionId),
    // 会话内容在本次交互内变化频繁，不做长时间缓存
    staleTime: 0,
  });
}

/**
 * 发送 mutation 的全局标识。
 *
 * 面板和页面按钮是两个不同的 useMutation 调用点，
 * 靠 mutationKey + useIsMutating 才能共享「正在发送」状态。
 */
export const buddySendMutationKey = ["buddy", "send-message"] as const;

export function useSendBuddyMessage() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationKey: buddySendMutationKey,

    mutationFn: async (content: string): Promise<string> => {
      // 读取最新状态，避免闭包里拿到旧值
      const { buddyContext, activeChatSessionId, activeChatCourseId, openChatSession } =
        useBuddyStore.getState();

      // 把用户原文与当前上下文一起交给 adapter
      const request = buildSendRequest(content, buddyContext);

      // 契约 6 的会话按课程创建；换课后必须换会话，否则问题会发进旧课程
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

      await buddyApi.sendMessage(sessionId, request.body, request.options);
      return sessionId;
    },

    onSuccess: (sessionId) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.messages(sessionId) });
      void queryClient.invalidateQueries({ queryKey: ["recent-chat-sessions"] });
    },
  });
}

/**
 * 任意调用点触发的「正在发送」状态。
 * 面板与页面上的 AI Action 按钮是两个 useMutation 实例，靠 mutationKey 对齐。
 */
export function useIsBuddySending(): boolean {
  return useIsMutating({ mutationKey: buddySendMutationKey }) > 0;
}

/**
 * 最近一次发送失败的错误。
 * 取 submittedAt 最新的那条，避免旧的失败记录在新一轮发送后仍然显示。
 */
export function useBuddySendError(): AppError | null {
  const states = useMutationState({
    filters: { mutationKey: buddySendMutationKey },
    select: (mutation) => ({
      status: mutation.state.status,
      error: mutation.state.error,
      submittedAt: mutation.state.submittedAt,
    }),
  });

  const latest = [...states].sort((a, b) => b.submittedAt - a.submittedAt)[0];
  if (!latest || latest.status !== "error") return null;
  return toAppError(latest.error);
}

export type RecentChatSessionVM = {
  id: string;
  courseId: string;
  title: string;
  createdAt: string;
};

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
    (result.data ?? []).forEach((session: ChatSessionDto) => {
      sessions.push({
        id: session.id,
        courseId,
        title: session.title,
        createdAt: session.created_at,
      });
    });
  });

  sessions.sort((a, b) => b.createdAt.localeCompare(a.createdAt));

  return {
    sessions: sessions.slice(0, limit),
    isPending: results.some((result) => result.isPending),
  };
}
