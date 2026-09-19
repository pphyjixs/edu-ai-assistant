/** 骨架屏。规格要求用 Skeleton 而不是全屏 spinner 表达加载态。 */

import { cn } from "@/components/utils";

import styles from "./Skeleton.module.css";

export type SkeletonProps = {
  width?: string | number;
  height?: string | number;
  radius?: string;
  className?: string;
};

export function Skeleton({ width, height = 12, radius, className }: SkeletonProps) {
  return (
    <span
      aria-hidden="true"
      className={cn(styles.skeleton, className)}
      style={{ width, height, borderRadius: radius }}
    />
  );
}

/** 一段多行文本占位 */
export function SkeletonLines({ lines = 3 }: { lines?: number }) {
  return (
    <span className={styles.lines}>
      {Array.from({ length: lines }, (_, index) => (
        <Skeleton
          key={index}
          height={10}
          width={index === lines - 1 ? "62%" : "100%"}
        />
      ))}
    </span>
  );
}
