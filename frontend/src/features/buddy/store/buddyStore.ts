/**
 * 全站 UI 状态（Zustand）。
 *
 * 遵守 DEVELOPMENT_SPEC.md 第 21 节的意图：这里只放 UI 状态，
 * 课程、资料、作业、成绩等业务数据全部由 TanStack Query 管理。
 *
 * 相对规格的 4 个字段多了一个 ``activeChatCourseId``：契约 6 的会话是按课程
 * 创建的，只记 sessionId 无法判断当前会话是否属于当前课程，会在切换课程后
 * 把新问题发进旧课程的会话里。它是 UI 侧的归属标记，不是业务数据。
 */

import { create } from "zustand";

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

export const useBuddyStore = create<BuddyState>((set) => ({
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
  clearChatSession: () => set({ activeChatSessionId: undefined, activeChatCourseId: undefined }),
  setCourseSidebarCollapsed: (collapsed) => set({ courseSidebarCollapsed: collapsed }),
  toggleCourseSidebar: () =>
    set((state) => ({ courseSidebarCollapsed: !state.courseSidebarCollapsed })),
}));
