/**
 * 页面级 AI Action 按钮。
 *
 * 统一使用轻量「✦ + 短文案」样式，不做成巨大按钮（DEVELOPMENT_SPEC 第 6.1 节）。
 * 点击行为分两类：
 * - ``prompt``：打开面板 → 写入上下文 → 发送预设问题；
 * - ``route``：该动作属于正式领域能力（例如自动出题走 Practice 流程），直接跳过去，
 *   不在对话里伪造结果。
 */

import { Icon } from "@/components/Icon/Icon";
import type { AgentRunActionDto } from "@/features/buddy/api";
import { useAskBuddy, useBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { resolveActionRoute, type AgentAction } from "@/features/buddy/model/actions";
import type { BuddyContext } from "@/features/buddy/model/types";
import { useNavigate } from "react-router-dom";

import styles from "./AgentActionButton.module.css";

export type AgentActionButtonProps = {
  label: string;
  /** 与 route 二选一 */
  prompt?: string;
  /** 领域动作的目标路径 */
  route?: string;
  /** 后端动作：决定加载什么上下文、用哪套提示词模板；省略按 ASK */
  agentAction?: AgentRunActionDto;
  hint?: string;
  /** 需要在点击瞬间补进上下文的字段 */
  contextPatch?: Partial<BuddyContext>;
  className?: string;
};

export function AgentActionButton({
  label,
  prompt,
  route,
  agentAction = "ASK",
  hint,
  contextPatch,
  className,
}: AgentActionButtonProps) {
  const askBuddy = useAskBuddy();
  const context = useBuddyContext();
  const navigate = useNavigate();

  const target = route ? resolveActionRoute({ label, route }, context.courseId) : null;

  return (
    <button
      type="button"
      className={[styles.action, className].filter(Boolean).join(" ")}
      title={hint ?? (prompt ?? "")}
      onClick={() => {
        if (route) {
          if (target) navigate(target);
          return;
        }
        if (prompt) askBuddy(prompt, contextPatch, agentAction);
      }}
    >
      <Icon name="spark" size={13} />
      <span>{label}</span>
    </button>
  );
}

/** 供页面批量渲染时的类型收窄 */
export function isRoutableAction(action: AgentAction): boolean {
  return Boolean(action.route);
}
