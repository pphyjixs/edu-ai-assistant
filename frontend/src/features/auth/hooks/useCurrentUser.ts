/** 当前用户。角色只在 UI 层控制菜单与按钮显隐，真实权限仍由后端校验。 */

import { useQuery } from "@tanstack/react-query";

import { queryKeys } from "@/services/queryKeys";

import { authApi } from "../api";

export type CurrentUserVM = {
  id: string;
  displayName: string;
  email: string;
  /** 视图模型用可读字符串，避免页面直接依赖后端枚举大小写 */
  role: "teacher" | "student";
  roleLabel: string;
  initial: string;
};

export function useCurrentUser() {
  return useQuery({
    queryKey: queryKeys.me,
    queryFn: () => authApi.getCurrentUser(),
    select: (profile): CurrentUserVM => ({
      id: profile.id,
      displayName: profile.display_name,
      email: profile.email,
      role: profile.role === "TEACHER" ? "teacher" : "student",
      roleLabel: profile.role === "TEACHER" ? "教师" : "学生",
      initial: profile.display_name.slice(0, 1).toUpperCase(),
    }),
    staleTime: Number.POSITIVE_INFINITY,
  });
}
