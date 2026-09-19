/**
 * 首页（及其他非课程页面）的布局：全局侧栏 + 单栏主区。
 *
 * 首页不常驻 Buddy 面板，右上角提供入口，面板以非模态抽屉出现
 * （DEVELOPMENT_SPEC 第 2.2 节 A）。
 */

import { Outlet } from "react-router-dom";

import { Button } from "@/components/Button/Button";
import { BuddyFab } from "@/features/buddy/components/BuddyFab/BuddyFab";
import { BuddyPanel } from "@/features/buddy/components/BuddyPanel/BuddyPanel";
import { useBuddyPanelControls, useBuddyOpen } from "@/features/buddy/hooks/useBuddy";

import { GlobalSidebar } from "./GlobalSidebar";
import { TopBar } from "./TopBar";

import styles from "./DashboardLayout.module.css";

export function DashboardLayout() {
  const buddyOpen = useBuddyOpen();
  const { toggleBuddy } = useBuddyPanelControls();

  return (
    <div className={styles.shell}>
      <GlobalSidebar />

      <div className={styles.main}>
        <TopBar
          right={
            <Button
              variant="ghost"
              size="sm"
              iconLeft="spark"
              onClick={toggleBuddy}
              aria-expanded={buddyOpen}
            >
              {buddyOpen ? "收起 Buddy" : "Buddy"}
            </Button>
          }
        />
        <div className={styles.content}>
          <Outlet />
        </div>
      </div>

      <BuddyPanel variant="overlay" />
      <BuddyFab />
    </div>
  );
}
