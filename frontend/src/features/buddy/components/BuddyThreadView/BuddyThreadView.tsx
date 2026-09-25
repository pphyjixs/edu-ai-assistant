/**
 * Buddy 对话主体：上下文条 + 消息列表 + 建议提问 + 输入框。
 *
 * 两种形态共用**同一套**业务数据逻辑（开发方案 4.2）：
 *
 * - ``center``：首页中央会话页（``/chats/:sessionId``），消息在中央、输入框贴在底部；
 * - ``panel``：课程工作区的停靠面板/抽屉（由 ``BuddyPanel`` 加上标题栏与拖拽手柄）。
 *
 * 之所以单独抽出来，是为了避免"中央一套、面板一套"两份聊天实现——那样两边的
 * 空态、失败提示、引用展示与滚动行为迟早会分叉。
 *
 * ``variant`` 会继续传给四个子组件：中央形态下它们的**水平内边距都归零**，
 * 由 `.center` 统一提供——这样消息列表、建议提问与输入框的左右边缘完全对齐
 * （参考 ChatGPT 桌面版：一条居中的对话列，四段内容共用同一对边界）。
 *
 * ``isSending`` 与「是否有进行中的 Run」都在这里统一计算：Run 从 PENDING 到终态
 * 都要有反馈，否则用户会在两次轮询之间看到空档。
 */

import { isRunInFlight } from "@/features/buddy/api";
import { useActiveBuddyRun } from "@/features/buddy/hooks/useBuddyThread";
import { buddyQuickPrompts } from "@/features/buddy/model/actions";

import { BuddyComposer } from "../BuddyComposer/BuddyComposer";
import { BuddyContextBar } from "../BuddyContextBar/BuddyContextBar";
import { BuddyMessageList } from "../BuddyMessageList/BuddyMessageList";
import { BuddyQuickPrompts } from "../BuddyQuickPrompts/BuddyQuickPrompts";

import styles from "./BuddyThreadView.module.css";

export type BuddyThreadViewProps = {
  variant: "center" | "panel";
  sessionId: string | undefined;
  /** 是否显示建议提问；中央空态由页面自己渲染引导，这里默认不显示 */
  showQuickPrompts?: boolean;
  /** 进入页面时把焦点放到输入框（中央会话页） */
  autoFocusComposer?: boolean;
};

export function BuddyThreadView({
  variant,
  sessionId,
  showQuickPrompts = true,
  autoFocusComposer = false,
}: BuddyThreadViewProps) {
  const isCenter = variant === "center";
  const { isSubmitting, run } = useActiveBuddyRun();

  const runInFlight = isRunInFlight(run?.status);
  const isSending = isSubmitting || runInFlight;

  return (
    <div className={isCenter ? styles.center : styles.panel}>
      <div className={styles.context}>
        <BuddyContextBar variant={variant} />
      </div>

      {/* 中央形态仅在用户接近底部时自动滚动，避免把正在翻旧消息的用户拽回来 */}
      <BuddyMessageList
        variant={variant}
        sessionId={sessionId}
        isSending={isSending}
        stickToBottom={isCenter}
      />

      {showQuickPrompts ? (
        <BuddyQuickPrompts variant={variant} prompts={buddyQuickPrompts} />
      ) : null}

      <div className={styles.composerWrap}>
        <BuddyComposer
          variant={variant}
          autoFocus={autoFocusComposer}
          // 同一会话存在进行中 Run 时禁止再次提交（沿用后端单活跃 Run 规则）
          disableSubmit={isSending}
        />
      </div>
    </div>
  );
}
