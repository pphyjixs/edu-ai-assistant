/**
 * Buddy 面板宽度规则。
 *
 * 用户可以拖拽调整宽度，但必须留出正文空间：
 *
 * - **最小 320px**：再窄就看不清消息里的引用与卡片；
 * - **最大视口的一半**：Buddy 是"与内容并行"的协作层，不能把正文挤没。
 *
 * 上限取"视口一半"而不是固定像素，是为了在大屏上允许更宽、在小屏上自动收敛；
 * 视口变化（拉窗口、旋转屏幕）时也要重新收敛，否则会出现宽度大于可用空间、
 * 面板把正文整块挤出去的情况。
 */

/** 面板最小宽度（px） */
export const BUDDY_PANEL_MIN_WIDTH = 320;

/** 面板最大宽度占视口的比例 */
export const BUDDY_PANEL_MAX_VIEWPORT_RATIO = 0.5;

/** 视口一半（px）；视口极窄时至少给到最小宽度，避免上限小于下限 */
export function maxPanelWidth(viewportWidth: number): number {
  return Math.max(BUDDY_PANEL_MIN_WIDTH, Math.round(viewportWidth * BUDDY_PANEL_MAX_VIEWPORT_RATIO));
}

/** 把宽度收敛到 [最小, 视口一半] 区间内 */
export function clampPanelWidth(width: number, viewportWidth: number): number {
  return Math.min(Math.max(Math.round(width), BUDDY_PANEL_MIN_WIDTH), maxPanelWidth(viewportWidth));
}
