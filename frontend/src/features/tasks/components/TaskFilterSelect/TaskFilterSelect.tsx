/**
 * 任务列表右上角的筛选下拉。
 *
 * 视觉上刻意**没有边框**：它是一个"标签 + 当前值 ⌄"的轻量控件，
 * 页面里已经有课程芯片承担强视觉，再加两个带框的下拉会让标题行发花。
 *
 * 用原生 ``<select>``：键盘操作、移动端选择器、辅助技术语义都由浏览器提供，
 * 不需要为这点需求自己造一个下拉组件。
 */

import { Icon } from "@/components/Icon/Icon";

import styles from "./TaskFilterSelect.module.css";

export type TaskFilterOption<T extends string> = {
  value: T;
  label: string;
};

export type TaskFilterSelectProps<T extends string> = {
  /** 控件 id：与 ``label`` 关联，同时也是测试里的稳定定位点 */
  id: string;
  label: string;
  value: T;
  options: Array<TaskFilterOption<T>>;
  onChange: (value: T) => void;
};

export function TaskFilterSelect<T extends string>({
  id,
  label,
  value,
  options,
  onChange,
}: TaskFilterSelectProps<T>) {
  return (
    <div className={styles.wrap}>
      <label className={styles.label} htmlFor={id}>
        {label}
      </label>
      <span className={styles.control}>
        <select
          id={id}
          className={styles.select}
          value={value}
          onChange={(event) => onChange(event.target.value as T)}
        >
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <Icon name="chevron-down" size={14} className={styles.chevron} />
      </span>
    </div>
  );
}
