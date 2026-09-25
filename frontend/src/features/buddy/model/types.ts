/**
 * Buddy 前端上下文模型。
 *
 * 这是「UI Context」，不等同于后端最终 DTO（DEVELOPMENT_SPEC.md 第 9.2 节）。
 * 页面只声明「当前对象是谁」，由 features/buddy/api 决定如何转换成请求。
 */

export type BuddyEntityType =
  | "course"
  | "material"
  | "material-section"
  | "assignment"
  | "submission"
  | "practice"
  | "grade";

export type BuddyContext = {
  courseId?: string;
  entityType?: BuddyEntityType;
  entityId?: string;
  sectionId?: string;
  selectedText?: string;
  /** 当前路由，用于在没有业务对象时也能说明「你在哪一页」 */
  route: string;
};

export const emptyBuddyContext: BuddyContext = { route: "/" };

/**
 * Buddy 的承载面。
 *
 * ``HOME``：首页与中央会话页（`/`、`/chats/:sessionId`）——消息显示在中央，
 * 不弹出右侧抽屉；``COURSE_PANEL``：课程工作区的停靠面板。
 *
 * 「进行中的 Run 自动打开面板」只在 ``COURSE_PANEL`` 生效（开发方案 4.3）。
 */
export type BuddySurface = "HOME" | "COURSE_PANEL";

/** 上下文里可以携带的数据是否足够发起一次有意义的提问 */
export function hasUsableContext(context: BuddyContext): boolean {
  return Boolean(context.courseId || context.entityType || context.selectedText);
}
