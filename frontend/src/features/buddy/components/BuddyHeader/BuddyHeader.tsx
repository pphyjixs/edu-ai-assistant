/** 面板标题栏。关闭按钮的语义是「收起面板」，会话本身保留。 */

import { Button } from "@/components/Button/Button";
import { Icon } from "@/components/Icon/Icon";

import styles from "./BuddyHeader.module.css";

export type BuddyHeaderProps = {
  onClose: () => void;
};

export function BuddyHeader({ onClose }: BuddyHeaderProps) {
  return (
    <div className={styles.head}>
      <div className={styles.titleBlock}>
        <span className={styles.title}>
          <span className={styles.mark} aria-hidden="true">
            <Icon name="spark" size={12} />
          </span>
          StudyBuddy
        </span>
        <span className={styles.subtitle}>围绕当前课程上下文对话</span>
      </div>

      <Button
        variant="ghost"
        size="sm"
        iconLeft="chevron-left"
        onClick={onClose}
        aria-label="收起 Buddy 面板"
      />
    </div>
  );
}
