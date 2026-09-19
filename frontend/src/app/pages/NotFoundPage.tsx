/** 404 页面。用链接而不是按钮，避免把可交互元素嵌套进另一个可交互元素。 */

import { Link } from "react-router-dom";

import { EmptyState } from "@/components/EmptyState/EmptyState";

import styles from "./PlaceholderPage.module.css";

export function NotFoundPage() {
  return (
    <div className={styles.page}>
      <EmptyState
        illustration="empty-learning"
        title="页面不存在"
        description="链接可能已经失效，或者这个页面还没有实现。"
        action={
          <Link to="/" className={styles.buttonLink}>
            回到首页
          </Link>
        }
      />
    </div>
  );
}
