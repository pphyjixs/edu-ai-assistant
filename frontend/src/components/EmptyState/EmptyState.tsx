/**
 * 空状态。文案要说明「为什么空」和「接下来做什么」，
 * 而不是只写一句「暂无数据」（见 DEVELOPMENT_SPEC.md 第 18 节）。
 */

import type { ReactNode } from "react";

import { cn } from "@/components/utils";

import styles from "./EmptyState.module.css";

export const illustrationNames = [
  "course-ai",
  "course-db",
  "course-system",
  "empty-learning",
] as const;

export type IllustrationName = (typeof illustrationNames)[number];

export type EmptyStateProps = {
  title: string;
  description?: string;
  illustration?: IllustrationName;
  action?: ReactNode;
  className?: string;
};

export function EmptyState({
  title,
  description,
  illustration,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div className={cn(styles.empty, className)}>
      {illustration ? (
        <img
          className={styles.illustration}
          src={`/svg/illustrations/${illustration}.svg`}
          alt=""
          aria-hidden="true"
        />
      ) : null}
      <p className={styles.title}>{title}</p>
      {description ? <p className={styles.description}>{description}</p> : null}
      {action ? <div className={styles.action}>{action}</div> : null}
    </div>
  );
}
