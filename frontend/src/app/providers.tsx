/**
 * 应用级 Provider 装配。
 *
 * 只放全局必需的三件事：路由、服务端状态缓存。
 * Buddy 的 UI 状态由 Zustand 自己的 store 管理，不需要 Provider。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { BrowserRouter } from "react-router-dom";

/**
 * 默认策略：
 * - retry 1：失败重试一次，避免弱网下直接报错，但也不无限重试；
 * - refetchOnWindowFocus false：本应用多为表单式流程，切回窗口时静默刷新会打断输入。
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 30_000,
    },
    mutations: {
      retry: 0,
    },
  },
});

export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>{children}</BrowserRouter>
    </QueryClientProvider>
  );
}
