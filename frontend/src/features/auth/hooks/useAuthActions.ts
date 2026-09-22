/**
 * 登录、注册、注销的动作。
 *
 * 契约要点：
 * - 2.1 注册不返回令牌，注册成功后前端用同一凭证调用登录接口；
 * - 2.2 登录响应返回两个令牌，前端自行管理；
 * - 2.5 注销只撤销当前会话，成功后前端清除两个令牌；
 *      请求体里的 Refresh Token 不存在或不属于当前用户时返回 404，
 *      与「重复注销」等价，按已完成处理。
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { HttpError, tokenStorage, type TokenBundle } from "@/services/http";
import { queryKeys } from "@/services/queryKeys";

import {
  authApi,
  type LoginRequestDto,
  type LoginResponseDto,
  type RegisterRequestDto,
} from "../api";

function persistTokens(response: LoginResponseDto): TokenBundle {
  const tokens: TokenBundle = {
    accessToken: response.access_token,
    refreshToken: response.refresh_token,
  };
  tokenStorage.write(tokens);
  return tokens;
}

export function useLogin() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: LoginRequestDto) => authApi.login(body),
    onSuccess: (response) => {
      persistTokens(response);
      // 登录响应里的 user 是概要（没有邮箱与创建时间），完整资料从 /users/me 取
      void queryClient.invalidateQueries({ queryKey: queryKeys.me });
    },
  });
}

export function useRegister() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (body: RegisterRequestDto) => {
      await authApi.register(body);
      // 契约 2.1：注册成功后由前端用同一凭证登录
      return authApi.login({ email: body.email, password: body.password });
    },
    onSuccess: (response) => {
      persistTokens(response);
      void queryClient.invalidateQueries({ queryKey: queryKeys.me });
    },
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  return useMutation({
    mutationFn: async (): Promise<void> => {
      const tokens = tokenStorage.read();
      if (!tokens) return;
      try {
        await authApi.logout(tokens.refreshToken);
      } catch (error) {
        // 404 表示这个会话已经不存在（重复注销）或不属于当前用户；
        // 两种情况下本地清除即可，不需要向用户报错。
        if (error instanceof HttpError && error.status === 404) return;
        throw error;
      }
    },
    onSettled: () => {
      tokenStorage.clear();
      // 清空缓存，避免下一个账号看到上一个账号的课程与资料
      queryClient.clear();
      navigate("/login", { replace: true });
    },
  });
}
