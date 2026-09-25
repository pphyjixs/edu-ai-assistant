/**
 * 会话输入框。
 * Enter 发送、Shift + Enter 换行；发送失败时在输入框上方给出可读提示。
 *
 * 发送走的是 **Agent Run**：用户消息先落库、接口立刻返回，助手消息由轮询取回，
 * 因此发送期间可以直接输入下一次提问的草稿（``disableSubmit`` 只禁按钮、
 * 不禁输入框），不必盯着一个没有反馈的请求。
 *
 * 同一会话存在进行中的 Run 时禁止再次提交——沿用后端「每会话最多一个未结束
 * Run」的规则（开发方案 4.5）。
 */

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/Button/Button";
import { Icon } from "@/components/Icon/Icon";
import { SkillPicker } from "@/features/buddy/components/SkillPicker/SkillPicker";
import {
  useActiveBuddyRun,
  useIsBuddySending,
  useSendBuddyRun,
} from "@/features/buddy/hooks/useBuddyThread";

import styles from "./BuddyComposer.module.css";

const INPUT_ID = "buddy-composer-input";

export type BuddyComposerProps = {
  /** 进入页面时把焦点放到输入框（中央会话页） */
  autoFocus?: boolean;
  /** 存在进行中的 Run 时禁用提交（输入框仍可编辑草稿） */
  disableSubmit?: boolean;
  /** 中央形态去掉自带水平外边距，由 BuddyThreadView 统一对齐 */
  variant?: "center" | "panel";
};

export function BuddyComposer({
  autoFocus = false,
  disableSubmit = false,
  variant = "panel",
}: BuddyComposerProps) {
  const [value, setValue] = useState("");
  const [selectedSkillId, setSelectedSkillId] = useState("");
  const send = useSendBuddyRun();
  const isSending = useIsBuddySending();
  const { error, run } = useActiveBuddyRun();
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const runInFlight = run?.status === "RUNNING" || run?.status === "PENDING";
  const blocked = isSending || runInFlight || disableSubmit;
  const canSubmit = value.trim().length > 0 && !blocked;

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus();
  }, [autoFocus]);

  async function submit() {
    const text = value.trim();
    if (!text || blocked) return;
    // 输入框里的自由提问是 ASK；带上下文的动作按钮另有 action（见 model/actions.ts）
    try {
      await send.mutateAsync({ input: text, action: "ASK", selectedSkillId: selectedSkillId || undefined });
      setValue("");
    } catch {
      // 发送失败时**保留草稿**：错误由 useActiveBuddyRun 暴露在输入框上方，
      // 用户改一改就能重发，不需要重新打一遍（开发方案 4.5）
    }
  }

  return (
    <div
      className={
        variant === "center" ? `${styles.composer} ${styles.composerCenter}` : styles.composer
      }
    >
      {error ? (
        <p className={styles.error} role="alert">
          {error.message}
        </p>
      ) : null}

      <label className="srOnly" htmlFor={INPUT_ID}>
        向 Buddy 提问
      </label>
      <textarea
        id={INPUT_ID}
        ref={inputRef}
        className={styles.input}
        rows={3}
        placeholder="针对当前课程、作业或资料提问…"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            submit();
          }
        }}
      />

      <div className={styles.row}>
        <span className={styles.contextHint}>
          <Icon name="context" size={13} />
          {blocked ? "正在生成回答…" : "当前页面"}
        </span>
        <SkillPicker value={selectedSkillId} onChange={setSelectedSkillId} disabled={blocked} />
        <Button
          variant="primary"
          size="sm"
          iconLeft="send"
          onClick={submit}
          disabled={!canSubmit}
          aria-label="发送"
        />
      </div>
    </div>
  );
}
