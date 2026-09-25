/** 卡片容器。默认白底 + 轻边框 + 大圆角，是页面里唯一的“面”。 */

import type { HTMLAttributes, ReactNode } from "react";

import { cn } from "@/components/utils";

import styles from "./Card.module.css";

export type CardProps = HTMLAttributes<HTMLDivElement> & {
  /** 悬停时轻微抬升，用于可点击卡片 */
  interactive?: boolean;
  /** 紧凑内边距，用于列表行式卡片 */
  compact?: boolean;
  /**
   * 去掉内边距，给「行式列表容器」用。
   *
   * 行的左右内边距由行自己决定，容器只负责圆角与边框；否则行内 hover 背景
   * 永远铺不满卡片两侧，会露出白边。
   */
  flush?: boolean;
  children?: ReactNode;
};

export function Card({
  interactive = false,
  compact = false,
  flush = false,
  className,
  children,
  ...rest
}: CardProps) {
  return (
    <div
      {...rest}
      className={cn(
        styles.card,
        interactive && styles.interactive,
        compact && styles.compact,
        flush && styles.flush,
        className,
      )}
    >
      {children}
    </div>
  );
}
