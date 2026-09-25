/**
 * Vitest 全局初始化。
 *
 * jsdom 缺少几个浏览器 API（滚动、媒体查询），组件会用到它们，
 * 因此在这里补齐最小实现，避免测试因为环境差异而失败。
 */

import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

// 消息列表的自动滚动依赖 scrollIntoView；jsdom 不实现它
Element.prototype.scrollIntoView = vi.fn();

// Buddy 面板宽度、响应式行为会读取 matchMedia
if (typeof window !== "undefined" && typeof window.matchMedia !== "function") {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

beforeEach(() => {
  // 每个用例从干净的存储开始，避免登录态与上次会话串台
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
