/**
 * 当前用户（契约 2.7）。
 *
 * 角色只在 UI 层控制菜单与按钮显隐，真实权限边界始终在后端
 * （docs/modules.md 第 1 节）。
 */

import { useQuery } from "@tanstack/react-query";

import { tokenStorage } from "@/services/http";
import { queryKeys } from "@/services/queryKeys";

import { authApi, type UserProfileDto } from "../api";

export type CurrentUserVM = {
  id: string;
  displayName: string;
  email: string;
  /** 视图模型用可读字符串，页面不直接依赖后端枚举大小写 */
  role: "teacher" | "student";
  roleLabel: string;
  initial: string;
};

export function toCurrentUserVM(profile: UserProfileDto): CurrentUserVM {
  return {
    id: profile.id,
    displayName: profile.display_name,
    email: profile.email,
    role: profile.role === "TEACHER" ? "teacher" : "student",
    roleLabel: profile.role === "TEACHER" ? "教师" : "学生",
    initial: profile.display_name.slice(0, 1).toUpperCase(),
  };
}

/** 本地是否还留着令牌；没有令牌就不必发这个必然 401 的请求 */
export function hasStoredTokens(): boolean {
  return tokenStorage.read() !== null;
}

export function useCurrentUser() {
  return useQuery({
    queryKey: queryKeys.me,
    queryFn: ({ signal }) => authApi.me(signal),
    select: toCurrentUserVM,
    enabled: hasStoredTokens(),
    // 令牌失效走的是 401 链路，这里不要重试造成额外请求
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
  });
}
