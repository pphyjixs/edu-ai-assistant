/**
 * Auth 的 API 入口。
 *
 * 后端 auth 模块已实现（backend/app/modules/auth），接入登录流程后
 * 这里换成基于 services/http 的实现：
 *   http.get<UserProfileDto>("/users/me")
 * 页面与布局组件不需要改动。
 */

import type { AuthApi } from "./contracts";
import { mockAuthApi } from "./mock";

export const authApi: AuthApi = mockAuthApi;

export type { AuthApi, UserProfileDto, UserRoleDto } from "./contracts";
