/** 会话建议提问。与页面 AI Action 一样，点击即送入当前会话。 */

import { useAskBuddy } from "@/features/buddy/hooks/useBuddy";

import styles from "./BuddyQuickPrompts.module.css";

export type BuddyQuickPromptsProps = {
  prompts: string[];
};

export function BuddyQuickPrompts({ prompts }: BuddyQuickPromptsProps) {
  const askBuddy = useAskBuddy();

  if (prompts.length === 0) return null;

  return (
    <div className={styles.prompts}>
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
