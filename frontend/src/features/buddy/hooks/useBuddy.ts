/**
 * Buddy 的读取与写上下文 Hook。
 *
 * 页面用 ``useSetBuddyContext`` 声明「当前对象是谁」，
 * 用 ``useAskBuddy`` 触发一次预设提问——两者都不涉及请求组装。
 */

import { useCallback, useEffect } from "react";
import { useLocation } from "react-router-dom";

import { useBuddyStore } from "../store/buddyStore";
import type { AgentRunActionDto } from "../api";
import type { BuddyContext } from "../model/types";

import { useSendBuddyRun } from "./useBuddyThread";

export function useBuddyOpen(): boolean {
  return useBuddyStore((state) => state.buddyOpen);
}

export function useBuddyContext(): BuddyContext {
  return useBuddyStore((state) => state.buddyContext);
}

export function useBuddyPanelControls() {
  const openBuddy = useBuddyStore((state) => state.openBuddy);
  const closeBuddy = useBuddyStore((state) => state.closeBuddy);
  const toggleBuddy = useBuddyStore((state) => state.toggleBuddy);
  return { openBuddy, closeBuddy, toggleBuddy };
}

/**
 * 页面声明当前上下文。路由变化会自动同步，所以页面只需要关心业务对象。
 */
export function useSetBuddyContext(context: BuddyContext): void {
  const setBuddyContext = useBuddyStore((state) => state.setBuddyContext);
  const route = useLocation().pathname;

  const { courseId, entityType, entityId, sectionId, selectedText } = context;

  useEffect(() => {
    setBuddyContext({ courseId, entityType, entityId, sectionId, selectedText, route });
  }, [setBuddyContext, courseId, entityType, entityId, sectionId, selectedText, route]);
}

/**
 * AI Action 的统一入口：打开面板并创建一次 Agent Run。
 *
 * 顺序很重要——先写入上下文补丁，再发送，这样请求里带的是最新的上下文
 * （Run 的 ``context`` 字段就是从 store 里的 BuddyContext 翻译出来的）。
 *
 * ``action`` 决定后端加载什么上下文、用哪套提示词模板；省略时按 ``ASK``。
 */
export function useAskBuddy() {
  const openBuddy = useBuddyStore((state) => state.openBuddy);
  const send = useSendBuddyRun();

  return useCallback(
    (
      prompt: string,
      contextPatch?: Partial<BuddyContext>,
      action: AgentRunActionDto = "ASK",
    ) => {
      if (contextPatch) {
        const current = useBuddyStore.getState().buddyContext;
        useBuddyStore.getState().setBuddyContext({ ...current, ...contextPatch });
      }
      openBuddy();
      send.mutate({ input: prompt, action });
    },
    [openBuddy, send],
  );
}
