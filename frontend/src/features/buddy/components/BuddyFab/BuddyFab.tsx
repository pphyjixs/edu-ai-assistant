/**
 * Buddy 悬浮入口。面板折叠时出现，保证 Agent 始终一键可达
 * （DEVELOPMENT_SPEC 第 8.3 节）。
 */

import { Icon } from "@/components/Icon/Icon";
import { useBuddyStore } from "@/features/buddy/store/buddyStore";

import styles from "./BuddyFab.module.css";

export function BuddyFab() {
  const buddyOpen = useBuddyStore((state) => state.buddyOpen);
  const openBuddy = useBuddyStore((state) => state.openBuddy);

  if (buddyOpen) return null;

  return (
    <button type="button" className={styles.fab} onClick={openBuddy} aria-label="打开 Buddy 面板">
      <Icon name="spark" size={18} />
    </button>
  );
}
