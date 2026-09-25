/**
 * 「任务」页的视图模型与筛选逻辑。
 *
 * 数据源是各门课程的作业（契约第 8 节）：后端没有「跨课程任务」接口
 * （Dashboard 的 `pending_assignments` 固定只返回最近 5 条、不可分页），
 * 因此这里按课程分别读取后再在内存里聚合 —— 与首页「今日待办」的做法一致
 * （见 `features/dashboard/hooks/useDashboard.ts` 的同名说明）。
 *
 * 这一层刻意是**纯函数**：筛选、排序、计数都不碰 React，便于单测覆盖；
 * 页面只负责把状态接进去。
 *
 * 四种筛选可叠加（前三种是页面右上角的下拉，最后一种用课程芯片）：
 * - 活动类型（`activity`）：目前只有「实验任务」，练习/测验接入后在这里扩展；
 * - 状态（`status`）：待提交/进行中、已截止、已关闭、已归档，教师多一个「待发布」；
 * - 截止时间（`due`）：全部 / 今天 / 本周 / 一个月内 / 已逾期 / 无截止时间；
 * - 课程（`courseId`）：用页面上的课程芯片选择。
 */

import type { PillTone } from "@/components/Pill/Pill";
import type { AssignmentVM } from "@/features/assignments/model/types";
import { formatFullDateTime } from "@/utils/datetime";

/** 当前用户角色：同一份作业在教师与学生眼里的状态文案不同 */
export type TaskRole = "teacher" | "student";

/** 活动类型。练习/测验接入后在这里追加取值即可，筛选器会自动多出一个选项。 */
export type TaskActivityKind = "ASSIGNMENT";

export const ACTIVITY_LABELS: Record<TaskActivityKind, string> = {
  ASSIGNMENT: "实验任务",
};

/**
 * 任务在列表里的状态。
 *
 * 注意区分「已截止」（时间事实）与「已关闭」（状态事实）：
 * 契约 8.1 明确截止时间不会把状态改成 CLOSED，两者必须分开表达。
 */
export type TaskState = "draft" | "todo" | "expired" | "closed" | "archived";

const STATE_BY_ROLE: Record<
  TaskState,
  { teacher: string; student: string; tone: PillTone }
> = {
  draft: { teacher: "待发布", student: "草稿", tone: "warn" },
  todo: { teacher: "进行中", student: "待提交", tone: "warn" },
  expired: { teacher: "已截止", student: "已截止", tone: "neutral" },
  closed: { teacher: "已关闭", student: "已关闭", tone: "neutral" },
  archived: { teacher: "已归档", student: "已归档", tone: "neutral" },
};

export type TaskRowVM = {
  /** 作业 id */
  id: string;
  activity: TaskActivityKind;
  activityLabel: string;
  title: string;
  courseId: string;
  courseName: string;
  /** 行链接：课程内的作业详情 */
  href: string;
  state: TaskState;
  stateLabel: string;
  stateTone: PillTone;
  /** 原始截止时间，供筛选使用；无截止为 null */
  dueAt: string | null;
  /** 「2026.10.09 23:59」或「未设置」 */
  dueLabel: string;
  /** 「剩余 3 天」等实时剩余量 */
  remainingLabel: string;
  /** 契约 8.10 的可提交判断；教师视角恒为 false（教师不提交） */
  canSubmit: boolean;
};

function taskStateOf(assignment: AssignmentVM): TaskState {
  if (assignment.status === "draft") return "draft";
  if (assignment.status === "archived") return "archived";
  if (assignment.status === "closed") return "closed";
  if (assignment.remaining.expired && !assignment.allowLateSubmission) return "expired";
  return "todo";
}

export function toTaskRowVM(
  assignment: AssignmentVM,
  courseName: string,
  role: TaskRole,
): TaskRowVM {
  const state = taskStateOf(assignment);
  const meta = STATE_BY_ROLE[state];

  return {
    id: assignment.id,
    activity: "ASSIGNMENT",
    activityLabel: ACTIVITY_LABELS.ASSIGNMENT,
    title: assignment.title,
    courseId: assignment.courseId,
    courseName,
    href: `/courses/${assignment.courseId}/assignments/${assignment.id}`,
    state,
    stateLabel: role === "teacher" ? meta.teacher : meta.student,
    stateTone: meta.tone,
    dueAt: assignment.dueAt,
    dueLabel: assignment.dueAt ? formatFullDateTime(assignment.dueAt) : "未设置",
    remainingLabel: assignment.remaining.text,
    canSubmit: role === "student" && assignment.canSubmit,
  };
}

/* ------------------------------ 筛选 ------------------------------ */

export type ActivityFilter = "all" | TaskActivityKind;
export type DueFilter = "all" | "today" | "week" | "month" | "expired" | "none";

export const ACTIVITY_OPTIONS: Array<{ value: ActivityFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "ASSIGNMENT", label: ACTIVITY_LABELS.ASSIGNMENT },
];

export const DUE_OPTIONS: Array<{ value: DueFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "today", label: "今天截止" },
  { value: "week", label: "7 天内" },
  { value: "month", label: "30 天内" },
  { value: "expired", label: "已逾期" },
  { value: "none", label: "未设置截止" },
];

/** 状态筛选的取值就是行内状态本身，保证下拉选项与行标签用同一套文案 */
export type StatusFilter = "all" | TaskState;

/**
 * 状态筛选选项**随角色变化**。
 *
 * 学生看不到草稿作业（后端在查询层就排除了，契约 8.1），因此不给学生
 * 「待发布」这一项 —— 一个永远筛出空列表的选项只会让人以为页面坏了。
 * 文案直接取自 ``STATE_BY_ROLE``，与行内标签严格一致（学生「待提交」、
 * 教师「进行中」，同一个状态两种称呼）。
 */
export function buildStatusOptions(
  role: TaskRole,
): Array<{ value: StatusFilter; label: string }> {
  const states: TaskState[] =
    role === "teacher"
      ? ["todo", "draft", "expired", "closed", "archived"]
      : ["todo", "expired", "closed", "archived"];

  return [
    { value: "all", label: "全部" },
    ...states.map((state) => ({
      value: state as StatusFilter,
      label: STATE_BY_ROLE[state][role],
    })),
  ];
}

const DAY_MS = 24 * 60 * 60 * 1000;

function startOfDay(date: Date): Date {
  const copy = new Date(date);
  copy.setHours(0, 0, 0, 0);
  return copy;
}

/** 截止时间是否落在某个筛选档里。``now`` 可注入，便于测试跨天行为。 */
export function matchesDue(dueAt: string | null, filter: DueFilter, now = new Date()): boolean {
  if (filter === "all") return true;
  if (filter === "none") return dueAt === null;
  if (dueAt === null) return false;

  const due = new Date(dueAt);
  if (Number.isNaN(due.getTime())) return false;

  if (filter === "expired") return due.getTime() < now.getTime();
  // 今天：按**本地日历日**判断，而不是「未来 24 小时」——用户说的"今天"是日期概念
  if (filter === "today") return startOfDay(due).getTime() === startOfDay(now).getTime();
  if (filter === "week") {
    const diff = due.getTime() - now.getTime();
    return diff >= 0 && diff <= 7 * DAY_MS;
  }
  const diff = due.getTime() - now.getTime();
  return diff >= 0 && diff <= 30 * DAY_MS;
}

export type TaskFilters = {
  activity: ActivityFilter;
  due: DueFilter;
  /** 任务状态；选项随角色变化（学生没有「待发布」） */
  status: StatusFilter;
  /** ``null`` 表示「全部课程」 */
  courseId: string | null;
  now?: Date;
};

/**
 * 按活动类型 + 状态 + 截止时间筛选（**不含课程**）。
 *
 * 单独抽出来是为了给课程芯片算计数：芯片上的数字应当是"其他筛选条件下，
 * 这门课还有几条"，否则切换筛选后计数会与实际列表不一致。
 */
export function filterTasks(tasks: TaskRowVM[], filters: TaskFilters): TaskRowVM[] {
  const now = filters.now ?? new Date();
  return tasks.filter((task) => {
    if (filters.activity !== "all" && task.activity !== filters.activity) return false;
    if (filters.status !== "all" && task.state !== filters.status) return false;
    if (filters.courseId !== null && task.courseId !== filters.courseId) return false;
    return matchesDue(task.dueAt, filters.due, now);
  });
}

/** 课程芯片：全部课程 + 每门课的条数（按其他筛选条件计算） */
export type CourseChip = {
  courseId: string | null;
  name: string;
  count: number;
};

export function buildCourseChips(
  tasks: TaskRowVM[],
  filters: TaskFilters,
): CourseChip[] {
  const scoped = filterTasks(tasks, { ...filters, courseId: null });
  const counts = new Map<string, number>();
  const names = new Map<string, string>();

  for (const task of scoped) {
    counts.set(task.courseId, (counts.get(task.courseId) ?? 0) + 1);
    names.set(task.courseId, task.courseName);
  }

  // 顺序按课程名稳定排序，避免每次刷新芯片跳位
  const chips: CourseChip[] = [...counts.entries()]
    .map(([courseId, count]) => ({
      courseId,
      name: names.get(courseId) ?? "",
      count,
    }))
    .sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));

  return [
    { courseId: null, name: "全部", count: scoped.length },
    ...chips,
  ];
}

/**
 * 排序：可提交/进行中的在前 → 有截止时间的按截止升序 → 无截止时间的在后 →
 * 已关闭与已归档沉底。与首页「今日待办」的取向一致：先看要动手的。
 */
export function sortTasks(tasks: TaskRowVM[]): TaskRowVM[] {
  const weight = (task: TaskRowVM): number => {
    if (task.state === "todo" || task.state === "draft") return 0;
    if (task.state === "expired") return 1;
    if (task.state === "closed") return 2;
    return 3;
  };

  return [...tasks].sort((a, b) => {
    const byWeight = weight(a) - weight(b);
    if (byWeight !== 0) return byWeight;
    if (a.dueAt === null && b.dueAt === null) return 0;
    if (a.dueAt === null) return 1;
    if (b.dueAt === null) return -1;
    return a.dueAt.localeCompare(b.dueAt);
  });
}
