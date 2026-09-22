/**
 * 把 auth 的刷新与未认证处理注入 HTTP 层。
 *
 * ``services/http`` 不能反向依赖业务模块，因此刷新逻辑由这里注入。
 * 组件必须在 Router 与 QueryClientProvider 之内。
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { useNavigate } from "react-router-dom";

import { configureHttp, tokenStorage } from "@/services/http";

import { authApi } from "../api";

export function AuthSessionBridge() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  useEffect(() => {
    configureHttp({
      /** 契约 2.4：刷新成功只更新 Access Token，保留原 Refresh Token */
      refresh: async () => {
        const tokens = tokenStorage.read();
        if (!tokens) return null;
        try {
          const refreshed = await authApi.refresh(tokens.refreshToken);
          return refreshed.access_token;
        } catch {
          // 会话不存在、已过期或已撤销：交给下面的 unauthenticated 处理
          return null;
        }
      },

      /** 契约 2.4：刷新失败后清除两个令牌并跳转 /login */
      unauthenticated: () => {
        tokenStorage.clear();
        queryClient.clear();
        navigate("/login", { replace: true });
      },
    });
  }, [navigate, queryClient]);

  return null;
}
