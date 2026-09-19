/** 单条会话消息。助手消息展示来源引用与「是否有依据」的状态。 */

import { Icon } from "@/components/Icon/Icon";
import { Pill } from "@/components/Pill/Pill";
import type { ChatMessageDto } from "@/features/buddy/api";

import { BuddySourceCitation } from "../BuddySourceCitation/BuddySourceCitation";

import styles from "./BuddyMessage.module.css";

export type BuddyMessageProps = {
  message: ChatMessageDto;
};

export function BuddyMessage({ message }: BuddyMessageProps) {
  const isAssistant = message.role === "ASSISTANT";

  return (
    <article className={isAssistant ? styles.assistant : styles.user}>
      {isAssistant ? (
        <span className={styles.avatar} aria-hidden="true">
          <Icon name="spark" size={12} />
        </span>
      ) : null}

      <div className={styles.bubble}>
        <p className={styles.content}>{message.content}</p>

        {isAssistant && !message.grounded ? (
          <div className={styles.ungrounded}>
            <Pill tone="neutral">未在课程资料中找到依据</Pill>
          </div>
        ) : null}

        {isAssistant ? <BuddySourceCitation citations={message.citations} /> : null}
      </div>
    </article>
  );
}
