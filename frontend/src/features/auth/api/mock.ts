/**
 * Auth 的 feature 级 mock adapter。
 *
 * 本阶段不实现登录流程，因此直接返回一个符合契约 2.7 结构的用户。
 * 为了验收时能同时查看学生与教师两套课程菜单，角色可以通过
 * URL 查询参数切换：``?role=teacher`` / ``?role=student``（默认 student）。
 * 这不属于产品功能，接入真实登录后连同本文件一起移除。
 */

import type { AuthApi, UserProfileDto, UserRoleDto } from "./contracts";

const DELAY_MS = 260;

const MOCK_USER: UserProfileDto = {
  id: "8c1f4b1e-2a3d-4c5e-9f60-7b8d9e0a1c22",
  email: "student@example.com",
  display_name: "彭同学",
  role: "STUDENT",
  created_at: "2026-09-01T02:15:00Z",
};

function roleFromQuery(): UserRoleDto {
  if (typeof window === "undefined") return MOCK_USER.role;
  const value = new URLSearchParams(window.location.search).get("role");
  if (value === "teacher") return "TEACHER";
  if (value === "student") return "STUDENT";
  return MOCK_USER.role;
}

export const mockAuthApi: AuthApi = {
  async getCurrentUser(): Promise<UserProfileDto> {
    await new Promise((resolve) => window.setTimeout(resolve, DELAY_MS));
    const role = roleFromQuery();
    return {
      ...MOCK_USER,
      role,
      email: role === "TEACHER" ? "teacher@example.com" : MOCK_USER.email,
      display_name: role === "TEACHER" ? "王老师" : MOCK_USER.display_name,
    };
  },
};
