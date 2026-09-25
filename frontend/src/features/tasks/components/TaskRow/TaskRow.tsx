/**
 * 任务列表的一行。
 *
 * 整行是一个链接（与首页待办卡片同一取向：点哪里都能进详情），
 * 因此行尾的箭头只是装饰，不再单独做成按钮——避免一行里出现两个可点区域，
 * 键盘用户也不必走两遍 Tab。
 *
 * 行内信息按重要性排：标题 → 状态标签 + 课程 + 截止时间。
 * 状态标签带文字（颜色不作为唯一表达方式，见 acceptance.md 第 9 节）。
 */

import { Link } from "react-router-dom";

import { Icon } from "@/components/Icon/Icon";
import { Pill } from "@/components/Pill/Pill";

import type { TaskRowVM } from "../../model/types";

import styles from "./TaskRow.module.css";

export type TaskRowProps = {
  task: TaskRowVM;
};

export function TaskRow({ task }: TaskRowProps) {
  return (
    <Link to={task.href} className={styles.row}>
      <span className={styles.icon} aria-hidden="true">
        <Icon name="assignment" size={18} />
      </span>

      <span className={styles.main}>
        <span className={styles.title}>{task.title}</span>

        <span className={styles.meta}>
          <Pill tone={task.stateTone}>{task.stateLabel}</Pill>
          <span className={styles.metaItem}>
            <span className={styles.metaLabel}>课程：</span>
            {task.courseName}
          </span>
          <span className={styles.metaItem}>
            <span className={styles.metaLabel}>截止时间：</span>
            {task.dueLabel}
          </span>
        </span>
      </span>

      <Icon name="arrow-up-right" size={22} className={styles.arrow} />
    </Link>
  );
}
