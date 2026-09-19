/**
 * 页面级 AI Action 按钮。
 *
 * 统一使用轻量「✦ + 短文案」样式，不做成巨大按钮（DEVELOPMENT_SPEC 第 6.1 节）。
 * 点击后由 useAskBuddy 统一完成「打开面板 → 写入上下文 → 发送预设问题」。
 */

import { Icon } from "@/components/Icon/Icon";
import { useAskBuddy } from "@/features/buddy/hooks/useBuddy";
import type { BuddyContext } from "@/features/buddy/model/types";

import styles from "./AgentActionButton.module.css";

export type AgentActionButtonProps = {
  label: string;
  prompt: string;
  /** 需要在点击瞬间补进上下文的字段 */
  contextPatch?: Partial<BuddyContext>;
  className?: string;
};

export function AgentActionButton({
  label,
  prompt,
  contextPatch,
  className,
}: AgentActionButtonProps) {
  const askBuddy = useAskBuddy();

  return (
    <button
      type="button"
      className={[styles.action, className].filter(Boolean).join(" ")}
      onClick={() => askBuddy(prompt, contextPatch)}
    >
      <Icon name="spark" size={13} />
      <span>{label}</span>
    </button>
  );
}
