/**
 * Buddy 的读取与写上下文 Hook。
 *
 * 页面用 ``useSetBuddyContext`` 声明「当前对象是谁」，
 * 用 ``useAskBuddy`` 触发一次预设提问——两者都不涉及请求组装。
 */

import { useCallback, useEffect } from "react";
import { useLocation } from "react-router-dom";

import { useBuddyStore } from "../store/buddyStore";
import type { BuddyContext } from "../model/types";

import { useSendBuddyMessage } from "./useBuddyThread";

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
 * AI Action 的统一入口：打开面板并把预设问题送进当前会话。
 *
 * 顺序很重要——先写入上下文补丁，再发送，这样请求里带的是最新的上下文。
 */
export function useAskBuddy() {
  const openBuddy = useBuddyStore((state) => state.openBuddy);
  const send = useSendBuddyMessage();

  return useCallback(
    (prompt: string, contextPatch?: Partial<BuddyContext>) => {
      if (contextPatch) {
        const current = useBuddyStore.getState().buddyContext;
        useBuddyStore.getState().setBuddyContext({ ...current, ...contextPatch });
      }
      openBuddy();
      send.mutate(prompt);
    },
    [openBuddy, send],
  );
}
