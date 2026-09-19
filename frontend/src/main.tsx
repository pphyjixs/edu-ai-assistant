/**
 * 应用入口。
 *
 * 样式加载顺序固定为：设计令牌 → 全局基础样式 → 工具类。
 * 令牌必须最先加载，否则组件里引用 var(--…) 会拿到空值。
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "@/app/App";

import "@/styles/tokens.css";
import "@/styles/global.css";
import "@/styles/utilities.css";

const container = document.getElementById("root");

if (!container) {
  throw new Error("找不到 #root 挂载点，请检查 index.html。");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
