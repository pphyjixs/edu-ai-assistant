/**
 * 占位页。
 *
 * 用于本阶段不实现的模块路由（课程资料、学习、成绩、教师批改等）。
 * 明确写出「属于哪个模块、下一阶段实现」，避免点进来白屏，
 * 也避免用假数据把未完成的功能伪装成已完成。
 */

import { Button } from "@/components/Button/Button";
import { EmptyState } from "@/components/EmptyState/EmptyState";
import { useSetBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { useParams } from "react-router-dom";

import styles from "./PlaceholderPage.module.css";

export type PlaceholderPageProps = {
  title: string;
  description: string;
};

export function PlaceholderPage({ title, description }: PlaceholderPageProps) {
  const { courseId } = useParams<{ courseId: string }>();

  useSetBuddyContext({ courseId, route: "" });

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>{title}</h1>
      <EmptyState
        illustration="empty-learning"
        title="这一部分还没有实现"
        description={description}
        action={
          <Button variant="secondary" size="sm" onClick={() => window.history.back()}>
            返回上一页
          </Button>
        }
      />
    </div>
  );
}
