/**
 * 首页「待办」卡片的视图模型。
 *
 * Dashboard 聚合接口尚未实现（见 docs/local-development-agent-backend.md 第 3.1 节），
 * 因此首页不再使用示例数据，而是由前端基于**真实接口**组合：
 * 课程列表（GET /courses）+ 各课程的任务列表（GET /courses/{id}/assignments）。
 *
 * 契约里没有提交模块，所以这里不显示「已提交」这类前端无法知道的状态。
 */

import type { PillTone } from "@/components/Pill/Pill";
import type { AssignmentVM } from "@/features/assignments/model/types";

export type TodayTaskVM = {
  id: string;
  assignmentId: string;
  title: string;
  courseName: string;
  /** 任务状态（草稿 / 进行中 / 已关闭） */
  statusLabel: string;
  statusTone: PillTone;
  dueLabel: string;
  remainingLabel: string;
  /** 卡片右上角的小标签：待发布 / 临近截止 / 剩余 N 天 / 已截止 */
  badgeLabel: string;
  badgeTone: PillTone;
  /** 整张卡片可点击进入任务（DEVELOPMENT_SPEC 第 7.3 节） */
  href: string;
  actionLabel: string;
};

export function toTodayTaskVM(
  assignment: AssignmentVM,
  courseName: string,
): TodayTaskVM {
  const badge: { label: string; tone: PillTone } = (() => {
    if (assignment.status === "draft") return { label: "待发布", tone: "neutral" };
    if (assignment.status === "closed") return { label: "已关闭", tone: "neutral" };
    if (assignment.remaining.expired) return { label: "已截止", tone: "neutral" };
    if (assignment.remaining.urgent) return { label: "临近截止", tone: "warn" };
    return { label: assignment.remaining.text, tone: "info" };
  })();

  return {
    id: assignment.id,
    assignmentId: assignment.id,
    title: assignment.title,
    courseName,
    statusLabel: assignment.statusLabel,
    statusTone: assignment.statusTone,
    dueLabel: assignment.dueLabel,
    remainingLabel: assignment.remaining.text,
    badgeLabel: badge.label,
    badgeTone: badge.tone,
    href: `/courses/${assignment.courseId}/assignments/${assignment.id}`,
    actionLabel: assignment.status === "draft" ? "去发布" : "查看详情",
  };
}

/* ------------------------------ 汇总 ------------------------------ */

export type StudentSummaryVM = {
  /** 已发布且未到截止，或允许补交的任务数 */
  pendingCount: number;
  /** 已关闭的任务数 */
  closedCount: number;
  tasks: TodayTaskVM[];
};

export type TeacherSummaryVM = {
  /** 我创建且未归档的课程数 */
  activeCourseCount: number;
  /** 需要发布的草稿任务数 */
  draftCount: number;
  tasks: TodayTaskVM[];
};
