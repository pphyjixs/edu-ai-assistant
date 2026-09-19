/**
 * 首页问候区。
 * 眉题用当前日期，主标题回答「今天要做什么」这个问题
 * （DEVELOPMENT_SPEC 第 7.1 节）。
 */

import type { ReactNode } from "react";

import { formatEyebrowDate, greetingFor } from "@/utils/datetime";

import styles from "./GreetingHero.module.css";

export type GreetingHeroProps = {
  displayName: string;
  children?: ReactNode;
};

export function GreetingHero({ displayName, children }: GreetingHeroProps) {
  return (
    <section className={styles.hero}>
      <p className={styles.eyebrow}>{formatEyebrowDate()}</p>
      <h1 className={styles.title}>
        {greetingFor()}，{displayName}
      </h1>
      <p className={styles.subtitle}>
        课程、资料、作业和 Buddy 都在同一个工作空间里。
      </p>
      {children}
    </section>
  );
}
