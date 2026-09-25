/**
 * 「任务」页：跨课程的待办列表（左栏「任务」）。
 *
 * 与首页「今日待办」的区别：首页只给最紧急的 4 条摘要，这里给**全部**可筛选的任务，
 * 因此多了三个筛选下拉（活动类型 / 状态 / 截止时间）和一行课程芯片；
 * 两处共用同一份数据源与同一个视图模型，不会出现「首页说有、任务页找不到」的不一致。
 *
 * 其中「状态」的选项**随角色变化**：学生看不到草稿作业，因此不给他「待发布」；
 * 同一个状态在两种角色下的文案也不同（学生「待提交」＝教师「进行中」）。
 *
 * 教师视角只包含自己创建的课程：别人课里的作业不是他的待办，
 * 但他自己的草稿会以「待发布」出现（学生看不到草稿，契约 8.1）。
 *
 * 列表容器固定高度并内部滚动：任务多时页面本身不滚动，标题与筛选器始终可见，
 * 这是截图里的形态，也避免长列表把筛选器推出视口。
 */

import { useEffect, useState } from "react";

import { Card } from "@/components/Card/Card";
import { EmptyState } from "@/components/EmptyState/EmptyState";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { Skeleton } from "@/components/Skeleton/Skeleton";
import { useCurrentUser } from "@/features/auth/hooks/useCurrentUser";
import { useSetBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { toAppError } from "@/services/http";

import { TaskFilterSelect } from "../components/TaskFilterSelect/TaskFilterSelect";
import { TaskRow } from "../components/TaskRow/TaskRow";
import { useTasks } from "../hooks/useTasks";
import {
  ACTIVITY_OPTIONS,
  DUE_OPTIONS,
  buildCourseChips,
  buildStatusOptions,
  filterTasks,
  type ActivityFilter,
  type DueFilter,
  type StatusFilter,
  type TaskRole,
} from "../model/types";

import styles from "./TasksPage.module.css";

const SKELETON_ROWS = 5;

export function TasksPage() {
  const userQuery = useCurrentUser();
  const role: TaskRole = userQuery.data?.role === "teacher" ? "teacher" : "student";

  const { tasks, isPending, isError, error, refetch } = useTasks(role);

  const [activity, setActivity] = useState<ActivityFilter>("all");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [due, setDue] = useState<DueFilter>("all");
  const [courseId, setCourseId] = useState<string | null>(null);

  // 任务页没有具体业务对象：上下文条显示"任务"而不是上一页残留的课程
  useSetBuddyContext({ route: "" });

  const filters = { activity, status, due, courseId };
  const chips = buildCourseChips(tasks, filters);
  const visible = filterTasks(tasks, filters);
  const statusOptions = buildStatusOptions(role);
  const filtered =
    activity !== "all" || status !== "all" || due !== "all" || courseId !== null;

  // 选中的课程在新数据里消失时（退出课程、课程被删）回到「全部」，
  // 否则用户会停在一个永远为空、也看不到选中的筛选状态上
  useEffect(() => {
    if (courseId !== null && !chips.some((chip) => chip.courseId === courseId)) {
      setCourseId(null);
    }
  }, [chips, courseId]);

  // 角色变化（例如切换账号）后原来的状态取值可能已经不在选项里，一并归位
  useEffect(() => {
    if (!statusOptions.some((option) => option.value === status)) {
      setStatus("all");
    }
  }, [statusOptions, status]);

  function resetFilters() {
    setActivity("all");
    setStatus("all");
    setDue("all");
    setCourseId(null);
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>
          待办
          {isPending ? null : <span className={styles.count}>（{visible.length}）</span>}
        </h1>

        <div className={styles.filters}>
          <TaskFilterSelect
            id="tasks-activity-filter"
            label="活动类型"
            value={activity}
            options={ACTIVITY_OPTIONS}
            onChange={setActivity}
          />
          <TaskFilterSelect
            id="tasks-status-filter"
            label="状态"
            value={status}
            options={statusOptions}
            onChange={setStatus}
          />
          <TaskFilterSelect
            id="tasks-due-filter"
            label="截止时间"
            value={due}
            options={DUE_OPTIONS}
            onChange={setDue}
          />
        </div>
      </header>

      <div className={styles.chips} role="group" aria-label="按课程筛选任务">
        {chips.map((chip) => {
          const active = chip.courseId === courseId;
          return (
            <button
              key={chip.courseId ?? "all"}
              type="button"
              aria-pressed={active}
              className={active ? `${styles.chip} ${styles.chipActive}` : styles.chip}
              onClick={() => setCourseId(chip.courseId)}
            >
              {chip.name}
              {chip.courseId === null ? null : (
                <span className={styles.chipCount}>{chip.count}</span>
              )}
            </button>
          );
        })}
      </div>

      {isError ? (
        <ErrorState
          title="任务加载失败"
          message={toAppError(error).message}
          onRetry={refetch}
        />
      ) : isPending ? (
        <Card flush className={styles.listCard}>
          <div className={styles.skeletonList} aria-busy="true">
            {Array.from({ length: SKELETON_ROWS }, (_, index) => (
              <Skeleton key={index} height={62} radius="10px" />
            ))}
          </div>
        </Card>
      ) : visible.length === 0 ? (
        <EmptyState
          illustration="empty-learning"
          title={filtered ? "没有符合筛选条件的任务" : "暂时没有待办任务"}
          description={
            filtered
              ? "换一个课程或截止时间看看，也可以清除筛选查看全部任务。"
              : "教师发布实验任务后，会出现在这里。"
          }
          action={
            filtered ? (
              <button type="button" className={styles.clearButton} onClick={resetFilters}>
                清除筛选
              </button>
            ) : null
          }
        />
      ) : (
        <Card flush className={styles.listCard}>
          <div className={styles.list} role="list">
            {visible.map((task) => (
              <TaskRow key={task.id} task={task} />
            ))}
          </div>
        </Card>
      )}

      <p className={styles.note}>
        任务来自各门课程的实验作业；「已截止」是时间事实，「已关闭」是教师手动关闭的状态。
      </p>
    </div>
  );
}
