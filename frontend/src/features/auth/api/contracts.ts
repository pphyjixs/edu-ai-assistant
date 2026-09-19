/**
 * Auth 的 DTO 定义。字段严格按 docs/api-contract.md 第 2 节（认证接口）。
 *
 * 这一阶段不实现登录页与守卫（不在本次范围内），但仍然先把
 * 当前用户这条契约读通，避免后续接入真实登录时改布局组件。
 */

/** 契约 2.1 / UserRole：平台角色只有教师与学生两种 */
export type UserRoleDto = "TEACHER" | "STUDENT";

export type UserProfileDto = {
  id: string;
  email: string;
  display_name: string;
  role: UserRoleDto;
  created_at: string;
};

export interface AuthApi {
  /** 契约 2.7 GET /users/me */
  getCurrentUser(): Promise<UserProfileDto>;
}
