/**
 * 把 auth 的刷新与未认证处理注入 HTTP 层，并做**跨标签页登录同步**。
 *
 * ``services/http`` 不能反向依赖业务模块，因此刷新逻辑由这里注入。
 * 组件必须在 Router 与 QueryClientProvider 之内。
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { useNavigate } from "react-router-dom";

import { configureHttp, tokenStorage, TOKEN_KEY } from "@/services/http";

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

  /**
   * 跨标签页登录同步。
   *
   * 令牌存在 ``localStorage``（同一浏览器共享），而**每个标签页的内存身份是独立的**：
   * 在另一个标签页登录了别的账号后，本页界面还停在上一个账号，但新发出的请求会带上
   * 新账号的令牌 —— 学生页面因此会收到 ``403 ROLE_FORBIDDEN``（后端按设计判定
   * "当前调用者不是学生"）。这个现象在测试反馈里出现过。
   *
   * 修法：监听 ``storage`` 事件（**只在其他标签页改动时触发**，本页自己改不会触发，
   * 因此不会自我打断）。令牌一变就清空缓存，``useCurrentUser`` 会重新请求
   * ``/users/me``，界面身份随令牌一起收敛；令牌被清空（在别处退出）则直接回登录页。
   */
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      // key 为 null 表示另一个标签页调用了 clear()
      if (event.key !== null && event.key !== TOKEN_KEY) return;

      queryClient.clear();
      if (!tokenStorage.read()) {
        navigate("/login", { replace: true });
      }
    };

    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [navigate, queryClient]);

  return null;
}
