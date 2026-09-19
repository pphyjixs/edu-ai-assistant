import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * 前端构建配置。
 *
 * ``@`` 指向 ``src``，避免深层目录里出现 ``../../`` 这种相对路径。
 * 开发端口固定 5173，与后端 ``FRONTEND_ORIGINS`` 的默认白名单一致
 * （见 docs/deployment-vercel.md 与 backend/app/core/config.py）。
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: false,
  },
});
