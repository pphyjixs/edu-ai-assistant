/** 会话建议提问。与页面 AI Action 一样，点击即送入当前会话。 */

import { useBuddyAskAction } from "@/features/buddy/hooks/useBuddy";

import styles from "./BuddyQuickPrompts.module.css";

export type BuddyQuickPromptsProps = {
  prompts: string[];
  /** 中央形态去掉自带水平内边距，由 BuddyThreadView 统一对齐 */
  variant?: "center" | "panel";
};

export function BuddyQuickPrompts({
  prompts,
  variant = "panel",
}: BuddyQuickPromptsProps) {
  // 课程工作区点一下会展开停靠面板；首页/中央会话只发送，不弹抽屉
  const askBuddy = useBuddyAskAction();

  if (prompts.length === 0) return null;

  return (
    <div
      className={
        variant === "center" ? `${styles.prompts} ${styles.promptsCenter}` : styles.prompts
      }
    >
      {prompts.map((prompt) => (
        <button
          key={prompt}
          type="button"
          className={styles.prompt}
          onClick={() => askBuddy(prompt)}
        >
          {prompt}
        </button>
      ))}
    </div>
  );
}
