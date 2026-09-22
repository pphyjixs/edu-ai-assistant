/**
 * 角色守卫。
 *
 * 只控制页面能否进入；后端仍会对每个接口独立校验角色与课程成员身份。
 * 用在「教师专属」这类整页受限的路由上，而不是逐个按钮。
 */

import type { ReactNode } from "react";

import { useCurrentUser } from "@/features/auth/hooks/useCurrentUser";

import { GuardDenied, GuardLoading } from "./AuthGuard";

export type RoleGuardProps = {
  allow: Array<"teacher" | "student">;
  children: ReactNode;
  /** 不满足角色时的说明文案 */
  message?: string;
};

export function RoleGuard({ allow, children, message }: RoleGuardProps) {
  const userQuery = useCurrentUser();

  if (userQuery.isPending) return <GuardLoading />;

  const role = userQuery.data?.role;
  if (!role || !allow.includes(role)) {
    return <GuardDenied message={message ?? "这个页面只对特定角色开放。"} />;
  }

  return <>{children}</>;
}
