/**
 * 首页（及其他非课程页面）的布局：全局侧栏 + 单栏主区。
 *
 * 首页不常驻 Buddy 面板，右上角提供入口，面板以非模态抽屉出现
 * （DEVELOPMENT_SPEC 第 2.2 节 A）。
 *
 * **中央会话页（``/chats/:sessionId``）是例外**：消息就在中央，
 * 因此这里不再渲染抽屉与悬浮入口，顶栏的 Buddy 按钮也隐藏
 * （开发方案 4.5）。承载面固定为 ``HOME``，BuddyPanel 的
 * 「进行中 Run 自动打开」因此不会在这个布局下触发。
 *
 * **首页（``/``）同样不出现顶栏 Buddy 按钮**：中央输入框就是唯一入口，
 * 再放一个按钮属于功能重复。其他非课程页面（/courses、/tasks、/workspace）保留。
 */

import { useEffect } from "react";
import { Outlet, useLocation } from "react-router-dom";

import { Button } from "@/components/Button/Button";
import { BuddyFab } from "@/features/buddy/components/BuddyFab/BuddyFab";
import { BuddyPanel } from "@/features/buddy/components/BuddyPanel/BuddyPanel";
import {
  useBuddyPanelControls,
  useBuddyOpen,
  useSetBuddySurface,
} from "@/features/buddy/hooks/useBuddy";
import { useBuddyStore } from "@/features/buddy/store/buddyStore";

import { GlobalSidebar } from "./GlobalSidebar";
import { TopBar } from "./TopBar";

import styles from "./DashboardLayout.module.css";

export function DashboardLayout() {
  const location = useLocation();
  const buddyOpen = useBuddyOpen();
  const { toggleBuddy } = useBuddyPanelControls();
  const closeBuddy = useBuddyStore((state) => state.closeBuddy);

  // 非课程页面的承载面是 HOME：自动打开面板的逻辑在这里不生效
  useSetBuddySurface("HOME");

  // 中央会话页不出现任何浮层入口
  const isHomeChat = location.pathname.startsWith("/chats/");

  // 首页的对话入口就是中央输入框，右上角再放一个 Buddy 按钮属于功能重复；
  // 其他非课程页面（/courses、/tasks…）保留原有入口。
  const isHome = location.pathname === "/";
  const showBuddyEntry = !isHomeChat && !isHome;
  const showTopBar = !isHomeChat && !isHome;

  // 从其他页面带着「已展开」的状态进来时不残留抽屉
  useEffect(() => {
    if (isHomeChat) closeBuddy();
  }, [isHomeChat, closeBuddy]);

  return (
    <div className={styles.shell}>
      <GlobalSidebar />

      <div className={styles.main}>
        {showTopBar ? (
          <TopBar
            right={
              showBuddyEntry ? (
                <Button
                  variant="ghost"
                  size="sm"
                  iconLeft="spark"
                  onClick={toggleBuddy}
                  aria-expanded={buddyOpen}
                >
                  {buddyOpen ? "收起 Buddy" : "Buddy"}
                </Button>
              ) : null
            }
          />
        ) : null}
        <div className={styles.content}>
          <Outlet />
        </div>
      </div>

      {showBuddyEntry ? (
        <>
          <BuddyPanel variant="overlay" />
          <BuddyFab />
        </>
      ) : null}
    </div>
  );
}
