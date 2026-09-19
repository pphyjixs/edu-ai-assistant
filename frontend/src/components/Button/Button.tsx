/**
 * 通用按钮。变体取自 DEVELOPMENT_SPEC 的按钮层级：
 * primary（深色主操作）、secondary（白底轻边框次操作）、
 * ghost（无边框，用于图标/工具按钮）、text（纯文字链接式操作）。
 */

import type { ButtonHTMLAttributes, ReactNode } from "react";

import { Icon, type IconName } from "@/components/Icon/Icon";
import { cn } from "@/components/utils";

import styles from "./Button.module.css";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "text";
export type ButtonSize = "sm" | "md";

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  iconLeft?: IconName;
  iconRight?: IconName;
  block?: boolean;
  children?: ReactNode;
};

export function Button({
  variant = "secondary",
  size = "md",
  iconLeft,
  iconRight,
  block = false,
  className,
  type = "button",
  children,
  ...rest
}: ButtonProps) {
  const iconSize = size === "sm" ? 14 : 15;

  return (
    <button
      {...rest}
      type={type}
      className={cn(
        styles.button,
        styles[variant],
        styles[size],
        block && styles.block,
        !children && styles.iconOnly,
        className,
      )}
    >
      {iconLeft ? <Icon name={iconLeft} size={iconSize} /> : null}
      {children ? <span className={styles.label}>{children}</span> : null}
      {iconRight ? <Icon name={iconRight} size={iconSize} /> : null}
    </button>
  );
}
