/**
 * 状态标签。设计规格要求「颜色不是表达状态的唯一方式」，
 * 因此 Pill 必须带文字，不使用纯色圆点代替语义。
 */

import type { ReactNode } from "react";

import { cn } from "@/components/utils";

import styles from "./Pill.module.css";

export type PillTone = "neutral" | "info" | "warn" | "success" | "danger" | "agent";

export type PillProps = {
  tone?: PillTone;
  className?: string;
  children: ReactNode;
};

export function Pill({ tone = "neutral", className, children }: PillProps) {
  return <span className={cn(styles.pill, styles[tone], className)}>{children}</span>;
}
