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

import { emptyBuddyContext, type BuddyContext, type BuddySurface } from "../model/types";

type BuddyState = {
  /** Buddy 面板是否展开；折叠后保留会话，仅隐藏面板 */
  buddyOpen: boolean;
  /**
   * 当前 Buddy 的承载面（surface）。
   *
   * 首页中央会话是 ``HOME``，课程工作区的停靠面板是 ``COURSE_PANEL``。
   * 「进行中的 Run 自动打开面板」只在 ``COURSE_PANEL`` 生效——否则首页
   * 会在发送消息或刷新恢复 Run 时弹出右侧抽屉，与中央会话并存（本轮要修的
   * 用户可见问题之一）。
   */
  activeSurface: BuddySurface;
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
  setActiveSurface: (surface: BuddySurface) => void;
  setBuddyContext: (context: BuddyContext) => void;
  /** 打开一个已存在的会话（如从侧栏最近对话进入） */
  openChatSession: (sessionId: string, courseId: string) => void;
  /**
   * 新建对话：只清掉当前会话 id，**保留** ``activeChatCourseId``
   * （它是「最近选择的课程」，首页据此预选课程）。
   */
  clearChatSession: () => void;
  setCourseSidebarCollapsed: (collapsed: boolean) => void;
  toggleCourseSidebar: () => void;
  /**
   * 面板宽度（px）。``null`` 表示用设计默认值，用户拖拽过之后才是具体数字。
   * 只存数字：区间收敛（最小 320、最大视口一半）由 model/panelWidth 负责，
   * 存储层不做业务判断。
   */
  buddyPanelWidth: number | null;
  setBuddyPanelWidth: (width: number) => void;
};

export const useBuddyStore = create<BuddyState>()(
  persist(
    (set) => ({
      buddyOpen: false,
      activeSurface: "HOME",
      buddyContext: emptyBuddyContext,
      activeChatSessionId: undefined,
      activeChatCourseId: undefined,
      courseSidebarCollapsed: false,
      buddyPanelWidth: null,

      openBuddy: () => set({ buddyOpen: true }),
      closeBuddy: () => set({ buddyOpen: false }),
      toggleBuddy: () => set((state) => ({ buddyOpen: !state.buddyOpen })),
      setActiveSurface: (surface) => set({ activeSurface: surface }),
      setBuddyContext: (context) => set({ buddyContext: context }),
      openChatSession: (sessionId, courseId) =>
        set({ activeChatSessionId: sessionId, activeChatCourseId: courseId }),
      clearChatSession: () => set({ activeChatSessionId: undefined }),
      setCourseSidebarCollapsed: (collapsed) => set({ courseSidebarCollapsed: collapsed }),
      toggleCourseSidebar: () =>
        set((state) => ({ courseSidebarCollapsed: !state.courseSidebarCollapsed })),
      setBuddyPanelWidth: (width) => set({ buddyPanelWidth: width }),
    }),
    {
      name: "buddy-active-session",
      storage: createJSONStorage(() => sessionStorage),
      // 只记住"当前会话是哪一条"与用户调过的面板宽度；
      // 面板开合与上下文交给页面重新声明
      partialize: (state) => ({
        activeChatSessionId: state.activeChatSessionId,
        activeChatCourseId: state.activeChatCourseId,
        buddyPanelWidth: state.buddyPanelWidth,
      }),
    },
  ),
);
