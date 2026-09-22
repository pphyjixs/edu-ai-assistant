/**
 * 会话输入框。
 * Enter 发送、Shift + Enter 换行；发送失败时在输入框上方给出可读提示。
 *
 * 发送走的是 **Agent Run**：用户消息先落库、接口立刻返回，助手消息由面板轮询取回，
 * 因此发送期间可以直接输入下一次提问的草稿，不必盯着一个没有反馈的请求。
 */

import { useState } from "react";

import { Button } from "@/components/Button/Button";
import { Icon } from "@/components/Icon/Icon";
import {
  useActiveBuddyRun,
  useIsBuddySending,
  useSendBuddyRun,
} from "@/features/buddy/hooks/useBuddyThread";

import styles from "./BuddyComposer.module.css";

const INPUT_ID = "buddy-composer-input";

export function BuddyComposer() {
  const [value, setValue] = useState("");
  const send = useSendBuddyRun();
  const isSending = useIsBuddySending();
  const { error, run } = useActiveBuddyRun();

  const canSubmit = value.trim().length > 0 && !isSending;

  function submit() {
    const text = value.trim();
    if (!text || isSending) return;
    // 输入框里的自由提问是 ASK；带上下文的动作按钮另有 action（见 model/actions.ts）
    send.mutate({ input: text, action: "ASK" });
    setValue("");
  }

  return (
    <div className={styles.composer}>
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
        className={styles.input}
        rows={3}
        placeholder="针对当前课程、作业或资料提问…"
        value={value}
        disabled={isSending}
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
          {run?.status === "RUNNING" || run?.status === "PENDING"
            ? "正在生成回答…"
            : "当前页面"}
        </span>
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
