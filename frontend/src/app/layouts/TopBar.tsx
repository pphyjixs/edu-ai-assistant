/** 顶栏外壳。左右两个插槽，具体内容由各 Layout / 页面决定。 */

import type { ReactNode } from "react";

import styles from "./TopBar.module.css";

export type TopBarProps = {
  left?: ReactNode;
  right?: ReactNode;
  /** 顶栏下方是否需要 1px 分隔线（首页不需要，工作区需要） */
  divided?: boolean;
};

export function TopBar({ left, right, divided = false }: TopBarProps) {
  return (
    <header className={divided ? `${styles.bar} ${styles.divided}` : styles.bar}>
      <div className={styles.left}>{left}</div>
      <div className={styles.right}>{right}</div>
    </header>
  );
}
