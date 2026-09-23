/**
 * 拖拽调整 Buddy 面板宽度。
 *
 * 交互与无障碍：
 *
 * - 指针拖拽（`pointerdown` + `setPointerCapture`）：拖到哪算哪，不会因为
 *   指针移出窗口而中断；
 * - **键盘可用**：手柄是 `role="separator"`，左右方向键各调 24px，
 *   Home / End 跳到最小 / 最大 —— 只用键盘的用户也能调整；
 * - 宽度收敛到 [320px, 视口一半]，并在窗口尺寸变化时重新收敛。
 *
 * 宽度写进 Buddy 的 UI store（随会话持久化），这样面板收起再打开、
 * 或同一个标签页刷新后都还是用户调过的那一档。
 */

import { useCallback, useEffect } from "react";

import { useBuddyStore } from "../store/buddyStore";
import { clampPanelWidth } from "../model/panelWidth";

/** 键盘每次调整的步长（px） */
const KEYBOARD_STEP = 24;

export function useBuddyPanelResize() {
  const width = useBuddyStore((state) => state.buddyPanelWidth);
  const setWidth = useBuddyStore((state) => state.setBuddyPanelWidth);

  // 视口变化时重新收敛：窗口变小后原来的宽度可能超过"视口一半"
  useEffect(() => {
    if (width === null) return;
    const onResize = () => {
      const next = clampPanelWidth(width, window.innerWidth);
      if (next !== width) setWidth(next);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [width, setWidth]);

  /** 把"指针位置"换算成宽度：面板贴右边缘，因此宽度 = 视口宽 - 指针 x */
  const resizeTo = useCallback(
    (clientX: number) => {
      setWidth(clampPanelWidth(window.innerWidth - clientX, window.innerWidth));
    },
    [setWidth],
  );

  const onHandlePointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      // 只响应主键（避免右键菜单/中键拖动也触发）
      if (event.button !== 0) return;
      event.preventDefault();
      const handle = event.currentTarget;
      handle.setPointerCapture(event.pointerId);

      const onMove = (moveEvent: PointerEvent) => resizeTo(moveEvent.clientX);
      const onUp = (upEvent: PointerEvent) => {
        handle.releasePointerCapture(upEvent.pointerId);
        handle.removeEventListener("pointermove", onMove);
        handle.removeEventListener("pointerup", onUp);
        handle.removeEventListener("pointercancel", onUp);
      };

      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
      handle.addEventListener("pointercancel", onUp);
      resizeTo(event.clientX);
    },
    [resizeTo],
  );

  const onHandleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      const current = width ?? clampPanelWidth(window.innerWidth * 0.25, window.innerWidth);
      let next: number | null = null;

      if (event.key === "ArrowLeft") next = current + KEYBOARD_STEP;
      else if (event.key === "ArrowRight") next = current - KEYBOARD_STEP;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = window.innerWidth;

      if (next === null) return;
      event.preventDefault();
      setWidth(clampPanelWidth(next, window.innerWidth));
    },
    [width, setWidth],
  );

  return { width, onHandlePointerDown, onHandleKeyDown };
}
