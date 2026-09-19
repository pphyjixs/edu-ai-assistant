/**
 * Buddy 面板。
 *
 * 两种形态：
 * - ``docked``：课程 Workspace 的第三栏，由 CourseWorkspaceLayout 控制列宽；
 * - ``overlay``：首页按需弹出的右侧抽屉（首页不常驻面板，见 DEVELOPMENT_SPEC 第 2.2 节）。
 *
 * 折叠只隐藏面板，会话与上下文都保留在 store / Query 中。
 */

import { cn } from "@/components/utils";
import { useIsBuddySending } from "@/features/buddy/hooks/useBuddyThread";
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
  const sessionId = useBuddyStore((state) => state.activeChatSessionId);
  const isSending = useIsBuddySending();

  if (!buddyOpen) return null;

  const panel = (
    <aside
      className={cn(styles.panel, variant === "overlay" && styles.overlay)}
      aria-label="StudyBuddy 助手面板"
    >
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
