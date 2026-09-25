import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/**
 * 前端构建与测试配置。
 *
 * ``@`` 指向 ``src``；``@contracts`` 指向由 OpenAPI 生成的类型产物
 * （生成文件禁止人工编辑，见 docs/collaboration.md 第 4 节）。
 * 开发端口固定 5173，与后端 ``FRONTEND_ORIGINS`` 的默认白名单一致
 * （见 docs/deployment-vercel.md 与 backend/app/core/config.py）。
 *
 * ``test`` 段（开发方案 10.3）：jsdom 环境 + 组件测试基础设施。
 * 测试文件与被测组件同目录，命名 ``*.test.tsx``；共享夹具放在 ``src/test``。
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
      "@contracts": fileURLToPath(new URL("../contracts/generated", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: false,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // CSS Modules 只需要属性名代理，不必真的编译样式
    css: false,
  },
});
