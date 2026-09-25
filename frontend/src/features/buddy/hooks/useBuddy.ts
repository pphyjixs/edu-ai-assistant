/**
 * Buddy 的读取与写上下文 Hook。
 *
 * 页面用 ``useSetBuddyContext`` 声明「当前对象是谁」，
 * 用 ``useAskBuddy`` / ``useSendBuddy`` 触发一次提问——两者都不涉及请求组装。
 *
 * **「发送」与「打开面板」是两件事**（开发方案 4.3）：
 *
 * - ``useAskBuddy``：发送 + 打开面板，保留给课程工作区的动作按钮；
 * - ``useSendBuddy``：只发送，不触碰任何展示状态——首页中央会话、
 *   中央会话页的快捷提示都走它，首页因此不会再弹出右侧抽屉。
 *
 * ``useBuddyAskAction`` 按当前承载面自动二选一，避免调用点各自判断。
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
 * 声明当前承载面（首页 / 中央会话 vs 课程工作区）。
 *
 * 面板的「进行中 Run 自动打开」只在 ``COURSE_PANEL`` 生效，因此每个布局都要
 * 显式声明一次，避免从课程页回到首页后残留上一个面。
 */
export function useSetBuddySurface(surface: "HOME" | "COURSE_PANEL"): void {
  const setActiveSurface = useBuddyStore((state) => state.setActiveSurface);
  useEffect(() => {
    setActiveSurface(surface);
  }, [setActiveSurface, surface]);
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

/**
 * 只发送、不打开任何展示容器。
 *
 * 首页中央输入框、中央会话页、首页快捷提示都用它：消息应当出现在中央，
 * 而不是再弹出一个右侧抽屉（开发方案 1 / 4.3）。
 */
export function useSendBuddy() {
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
      send.mutate({ input: prompt, action });
    },
    [send],
  );
}

/**
 * 按承载面选择「发送方式」：课程工作区打开停靠面板，首页/中央会话只发送。
 */
export function useBuddyAskAction() {
  const activeSurface = useBuddyStore((state) => state.activeSurface);
  const askBuddy = useAskBuddy();
  const sendBuddy = useSendBuddy();
  return activeSurface === "COURSE_PANEL" ? askBuddy : sendBuddy;
}
