/**
 * 图标。素材是交接包提供的原创线性图标（public/svg/icons，24×24、
 * stroke 1.7~1.9、使用 currentColor），因此这里用 CSS mask 渲染，
 * 既能继续复用 SVG 源文件，又能让图标跟随文字颜色。
 *
 * 图标默认是装饰性的（aria-hidden）。旁边没有文字标签时，
 * 通过 ``label`` 传一个可读名称，让它对辅助技术可见。
 */

import { cn } from "@/components/utils";

import styles from "./Icon.module.css";

export const iconNames = [
  "home",
  "courses",
  "tasks",
  "workspace",
  "assignment",
  "file",
  "grade",
  "search",
  "plus",
  "send",
  "spark",
  "upload",
  "context",
  "chevron-left",
  "chevron-down",
  "arrow-up-right",
] as const;

export type IconName = (typeof iconNames)[number];

export type IconProps = {
  name: IconName;
  size?: number;
  className?: string;
  /** 提供后图标对辅助技术可见，并作为可读名称 */
  label?: string;
};

export function Icon({ name, size = 20, className, label }: IconProps) {
  const url = `url(/svg/icons/${name}.svg)`;

  return (
    <span
      className={cn(styles.icon, className)}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      style={{
        width: size,
        height: size,
        maskImage: url,
        WebkitMaskImage: url,
      }}
    />
  );
}
