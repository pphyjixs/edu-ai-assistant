/**
 * 组件测试的渲染辅助。
 *
 * 提供三件事：
 *
 * 1. :func:`seedTokens` —— 让 ``AuthGuard`` 认为已经登录（真实权限在后端）；
 * 2. :func:`resetBuddyStore` —— 清掉上一个用例残留的 UI 状态；
 * 3. :func:`renderApp` —— 挂上 QueryClient 与 MemoryRouter。
 *
 * 刻意**不**复用 ``AppProviders``：它会创建 BrowserRouter、注册跨标签同步等
 * 与测试无关的副作用，且 QueryClient 是模块级单例（用例之间会互相污染）。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";

import { emptyBuddyContext } from "@/features/buddy/model/types";
import { useBuddyStore } from "@/features/buddy/store/buddyStore";
import { tokenStorage } from "@/services/http";

export function seedTokens(): void {
  tokenStorage.write({ accessToken: "test-access", refreshToken: "test-refresh" });
}

export function resetBuddyStore(): void {
  useBuddyStore.setState({
    buddyOpen: false,
    activeSurface: "HOME",
    buddyContext: emptyBuddyContext,
    activeChatSessionId: undefined,
    activeChatCourseId: undefined,
  });
}

export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      // 测试里不要重试：失败应当立刻暴露，而不是拖慢用例
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

export function renderApp(ui: ReactElement, { route = "/" }: { route?: string } = {}) {
  const client = createTestQueryClient();

  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
      </QueryClientProvider>
    );
  }

  return { client, ...render(ui, { wrapper: Wrapper }) };
}
