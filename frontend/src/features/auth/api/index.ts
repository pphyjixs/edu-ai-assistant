/**
 * Auth 接口（契约第 2 节）。
 *
 * 字段类型全部来自 OpenAPI 生成的 Schema，本文件只做「路径 + 方法」的编排，
 * 不再手写 DTO。
 */

import { http } from "@/services/http";
import type { Schemas } from "@/types/api";

export type UserProfileDto = Schemas["UserProfile"];
export type UserSummaryDto = Schemas["UserSummary"];
export type UserRoleDto = Schemas["UserRole"];
export type RegisterRequestDto = Schemas["RegisterRequest"];
export type LoginRequestDto = Schemas["LoginRequest"];
export type LoginResponseDto = Schemas["LoginResponse"];
export type RefreshResponseDto = Schemas["RefreshResponse"];

export const authApi = {
  /** 契约 2.1：POST /auth/register —— 201 只返回账号资料，不返回令牌 */
  register(body: RegisterRequestDto): Promise<UserProfileDto> {
    return http.post<UserProfileDto>("/auth/register", body, { auth: false });
  },

  /** 契约 2.1：POST /auth/login */
  login(body: LoginRequestDto): Promise<LoginResponseDto> {
    return http.post<LoginResponseDto>("/auth/login", body, { auth: false });
  },

  /**
   * 契约 2.4：POST /auth/refresh
   * 只需要 Refresh Token，不要求有效的 Access Token；
   * 响应只含新的 Access Token，原 Refresh Token 及到期时间不变。
   */
  refresh(refreshToken: string): Promise<RefreshResponseDto> {
    return http.post<RefreshResponseDto>(
      "/auth/refresh",
      { refresh_token: refreshToken },
      { auth: false },
    );
  },

  /**
   * 契约 2.5：POST /auth/logout
   * 需要有效 Access Token 并在请求体里指定要撤销的会话。
   * Access Token 已过期时，http 层会先刷新再重放这次注销。
   */
  logout(refreshToken: string): Promise<void> {
    return http.post<void>("/auth/logout", { refresh_token: refreshToken });
  },

  /** 契约 2.7：GET /users/me */
  me(signal?: AbortSignal): Promise<UserProfileDto> {
    return http.get<UserProfileDto>("/users/me", { signal });
  },
};
