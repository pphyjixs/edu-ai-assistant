/**
 * Buddy 面板。
 *
 * 两种形态：
 * - ``docked``：课程 Workspace 的第三栏，由 CourseWorkspaceLayout 控制列宽；
 * - ``overlay``：首页按需弹出的右侧抽屉（首页不常驻面板，见 DEVELOPMENT_SPEC 第 2.2 节）。
 *
 * 折叠只隐藏面板，会话与上下文都保留在 store / Query 中。
 *
 * **宽度可拖拽**：最小 320px、最大视口一半。停靠形态下宽度由布局的网格列决定，
 * 浮层与窄屏形态下由面板自己决定 —— 两处读的都是同一个 store 值，
 * 所以拖动时两种形态的表现一致。
 */

import { useEffect } from "react";

import { cn } from "@/components/utils";
import { isRunInFlight } from "@/features/buddy/api";
import { useActiveBuddyRun } from "@/features/buddy/hooks/useBuddyThread";
import { useBuddyPanelResize } from "@/features/buddy/hooks/useBuddyPanelResize";
import { BUDDY_PANEL_MIN_WIDTH, maxPanelWidth } from "@/features/buddy/model/panelWidth";
import { useBuddyStore } from "@/features/buddy/store/buddyStore";
import { buddyQuickPrompts } from "@/features/buddy/model/actions";

import { BuddyComposer } from "../BuddyComposer/BuddyComposer";
import { BuddyContextBar } from "../BuddyContextBar/BuddyContextBar";
import { BuddyHeader } from "../BuddyHeader/BuddyHeader";
import { BuddyMessageList } from "../BuddyMessageList/BuddyMessageList";
import { BuddyQuickPrompts } from "../BuddyQuickPrompts/BuddyQuickPrompts";

import styles from "./BuddyPanel.module.css";

export type BuddyPanelProps = {
  variant?: "docked" | "overlay";
};

export function BuddyPanel({ variant = "docked" }: BuddyPanelProps) {
  const buddyOpen = useBuddyStore((state) => state.buddyOpen);
  const closeBuddy = useBuddyStore((state) => state.closeBuddy);
  const openBuddy = useBuddyStore((state) => state.openBuddy);
  const sessionId = useBuddyStore((state) => state.activeChatSessionId);
  const sessionCourseId = useBuddyStore((state) => state.activeChatCourseId);
  const pageCourseId = useBuddyStore((state) => state.buddyContext.courseId);
  // 宽度可拖拽；区间收敛（最小 320 / 最大视口一半）在 hook 与 model 里
  const { width, onHandlePointerDown, onHandleKeyDown } = useBuddyPanelResize();
  // 「等待中」不只在提交那一刻：Run 从 PENDING 到终态都要显示进度，
  // 否则用户会在两次轮询之间看到空档（文档 6.8）。
  const { isSubmitting, run } = useActiveBuddyRun();
  const isSending = isSubmitting || isRunInFlight(run?.status);

  // 刷新后如果**当前课程**还有没结束的 Run，就自动把面板打开：
  // 否则用户既看不到进度，也等不到那条回答（评审文档「一、#11」）。
  // 加上课程判断，避免在浏览别的课程时被上一次的会话打扰。
  useEffect(() => {
    if (!run || !isRunInFlight(run.status)) return;
    if (!pageCourseId || sessionCourseId !== pageCourseId) return;
    openBuddy();
  }, [run, openBuddy, sessionCourseId, pageCourseId]);

  if (!buddyOpen) return null;

  const panel = (
    <aside
      className={cn(styles.panel, variant === "overlay" && styles.overlay)}
      aria-label="StudyBuddy 助手面板"
      // 浮层与窄屏（≤1180px）时面板脱离网格流，宽度只能由它自己决定；
      // 停靠形态下宽度由布局的网格列控制（列宽同样读这个状态）
      style={
        width === null
          ? undefined
          : ({ "--buddy-panel-width": `${width}px` } as React.CSSProperties)
      }
    >
      {/*
        拖拽手柄贴在面板左缘：宽度可调，但正文永远留得住（上限是视口一半）。
        键盘同样能用：左右方向键微调，Home / End 跳到两端。
      */}
      <div
        className={styles.resizeHandle}
        role="separator"
        aria-orientation="vertical"
        aria-label="调整 Buddy 面板宽度"
        aria-valuemin={BUDDY_PANEL_MIN_WIDTH}
        aria-valuemax={
          typeof window === "undefined" ? undefined : maxPanelWidth(window.innerWidth)
        }
        aria-valuenow={width ?? undefined}
        tabIndex={0}
        onPointerDown={onHandlePointerDown}
        onKeyDown={onHandleKeyDown}
      />

      <BuddyHeader onClose={closeBuddy} />
      <BuddyContextBar />
      <BuddyMessageList sessionId={sessionId} isSending={isSending} />
      <BuddyQuickPrompts prompts={buddyQuickPrompts} />
      <BuddyComposer />
    </aside>
  );

  if (variant === "overlay") {
    // 刻意不加遮罩：Buddy 是与内容并行的协作层，不是挡住页面的模态弹窗
    return panel;
  }

  return panel;
}
