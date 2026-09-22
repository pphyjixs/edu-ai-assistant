/** 单条会话消息。助手消息展示来源引用与「是否有依据」的状态。 */

import { Icon } from "@/components/Icon/Icon";
import { Pill } from "@/components/Pill/Pill";
import { citationsOf, isAssistantMessage, type ChatMessageDto } from "@/features/buddy/api";

import { BuddySourceCitation } from "../BuddySourceCitation/BuddySourceCitation";

import styles from "./BuddyMessage.module.css";

export type BuddyMessageProps = {
  message: ChatMessageDto;
};

export function BuddyMessage({ message }: BuddyMessageProps) {
  const isAssistant = isAssistantMessage(message);
  const citations = citationsOf(message);

  return (
    <article className={isAssistant ? styles.assistant : styles.user}>
      {isAssistant ? (
        <span className={styles.avatar} aria-hidden="true">
          <Icon name="spark" size={12} />
        </span>
      ) : null}

      <div className={styles.bubble}>
        <p className={styles.content}>{message.content}</p>

        {/* grounded 为 null 表示这条不是问答回答（例如用户消息） */}
        {isAssistant && message.grounded === false ? (
          <div className={styles.ungrounded}>
            <Pill tone="neutral">未在课程资料中找到依据</Pill>
          </div>
        ) : null}

        {isAssistant ? <BuddySourceCitation citations={citations} /> : null}
      </div>
    </article>
  );
}
