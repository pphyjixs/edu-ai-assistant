/**
 * 未登录守卫。
 *
 * 只负责使用体验：没有令牌就跳登录页，并在登录后回到原页面。
 * 真正的权限边界始终在后端（docs/acceptance.md 第 1 节）。
 */

import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { Link } from "react-router-dom";

import { EmptyState } from "@/components/EmptyState/EmptyState";
import { Skeleton } from "@/components/Skeleton/Skeleton";
import { hasStoredTokens, useCurrentUser } from "@/features/auth/hooks/useCurrentUser";

import styles from "./Guards.module.css";

/** 加载态用骨架屏而不是全屏 spinner（DEVELOPMENT_SPEC 第 18 节） */
export function GuardLoading() {
  return (
    <div className={styles.loading} aria-busy="true">
      <Skeleton height={28} width="38%" />
      <Skeleton height={14} width="62%" />
      <div className={styles.loadingCards}>
        <Skeleton height={168} radius="14px" />
        <Skeleton height={168} radius="14px" />
        <Skeleton height={168} radius="14px" />
      </div>
    </div>
  );
}

export function GuardDenied({ message }: { message: string }) {
  return (
    <div className={styles.denied}>
      <EmptyState
        illustration="empty-learning"
        title="没有访问权限"
        description={message}
        action={
          <Link to="/" className={styles.backLink}>
            回到首页
          </Link>
        }
      />
    </div>
  );
}

export function AuthGuard({ children }: { children: ReactNode }) {
  const location = useLocation();
  const userQuery = useCurrentUser();

  if (!hasStoredTokens()) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  }

  if (userQuery.isPending) return <GuardLoading />;

  if (userQuery.isError || !userQuery.data) {
    // 401 已由 AuthSessionBridge 处理并跳转；这里覆盖的是其他失败情况
    return (
      <GuardDenied
        message={
          userQuery.isError
            ? "登录状态校验失败，请重新登录后再试。"
            : "未能读取当前用户信息。"
        }
      />
    );
  }

  return <>{children}</>;
}
