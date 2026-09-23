/**
 * 全站 UI 状态（Zustand）。
 *
 * 遵守 DEVELOPMENT_SPEC.md 第 21 节的意图：这里只放 UI 状态，
 * 课程、资料、作业、成绩等业务数据全部由 TanStack Query 管理。
 *
 * 相对规格的 4 个字段多了一个 ``activeChatCourseId``：契约 6 的会话是按课程
 * 创建的，只记 sessionId 无法判断当前会话是否属于当前课程，会在切换课程后
 * 把新问题发进旧课程的会话里。它是 UI 侧的归属标记，不是业务数据。
 *
 * **当前会话 id 会被持久化到 sessionStorage**（评审文档「一、#11」）：
 * 刷新页面后 store 是空的，如果不记住"上次是哪个会话"，
 * 就没有任何入口去查 ``active-run``，正在生成的 Run 会彻底从界面上消失。
 * 用 sessionStorage 而不是 localStorage：新标签页应当从干净状态开始。
 */

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

import { emptyBuddyContext, type BuddyContext } from "../model/types";

type BuddyState = {
  /** Buddy 面板是否展开；折叠后保留会话，仅隐藏面板 */
  buddyOpen: boolean;
  /** 当前页面声明的上下文，由页面写入、由 BuddyContextBar 展示 */
  buddyContext: BuddyContext;
  /** 当前会话 id */
  activeChatSessionId: string | undefined;
  /** 当前会话所属课程，用于判断换课后是否需要新建会话 */
  activeChatCourseId: string | undefined;
  /** 课程内左侧导航是否收起 */
  courseSidebarCollapsed: boolean;

  openBuddy: () => void;
  closeBuddy: () => void;
  toggleBuddy: () => void;
  setBuddyContext: (context: BuddyContext) => void;
  /** 打开一个已存在的会话（如从侧栏最近对话进入） */
  openChatSession: (sessionId: string, courseId: string) => void;
  clearChatSession: () => void;
  setCourseSidebarCollapsed: (collapsed: boolean) => void;
  toggleCourseSidebar: () => void;
};

export const useBuddyStore = create<BuddyState>()(
  persist(
    (set) => ({
      buddyOpen: false,
      buddyContext: emptyBuddyContext,
      activeChatSessionId: undefined,
      activeChatCourseId: undefined,
      courseSidebarCollapsed: false,

      openBuddy: () => set({ buddyOpen: true }),
      closeBuddy: () => set({ buddyOpen: false }),
      toggleBuddy: () => set((state) => ({ buddyOpen: !state.buddyOpen })),
      setBuddyContext: (context) => set({ buddyContext: context }),
      openChatSession: (sessionId, courseId) =>
        set({ activeChatSessionId: sessionId, activeChatCourseId: courseId }),
      clearChatSession: () =>
        set({ activeChatSessionId: undefined, activeChatCourseId: undefined }),
      setCourseSidebarCollapsed: (collapsed) => set({ courseSidebarCollapsed: collapsed }),
      toggleCourseSidebar: () =>
        set((state) => ({ courseSidebarCollapsed: !state.courseSidebarCollapsed })),
    }),
    {
      name: "buddy-active-session",
      storage: createJSONStorage(() => sessionStorage),
      // 只记住"当前会话是哪一条"；面板开合与上下文交给页面重新声明
      partialize: (state) => ({
        activeChatSessionId: state.activeChatSessionId,
        activeChatCourseId: state.activeChatCourseId,
      }),
    },
  ),
);
