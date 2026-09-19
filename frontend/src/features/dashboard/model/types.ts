/** 首页「今日待办」卡片的视图模型。 */

import type { PillTone } from "@/components/Pill/Pill";
import { formatMonthDayTime, formatRemaining, type Remaining } from "@/utils/datetime";

import type { DashboardTaskDto } from "../api";

export type TodayTaskVM = {
  id: string;
  assignmentId: string;
  title: string;
  courseName: string;
  dueLabel: string;
  remaining: Remaining;
  /** 卡片右上角的小标签：已提交 / 临近截止 / 剩余 N 天 / 已截止 */
  badgeLabel: string;
  badgeTone: PillTone;
  /** 整张卡片可点击进入任务（DEVELOPMENT_SPEC 第 7.3 节） */
  href: string;
  actionLabel: string;
};

function badgeFor(dto: DashboardTaskDto, remaining: Remaining): { label: string; tone: PillTone } {
  if (dto.submitted) return { label: "已提交", tone: "success" };
  if (remaining.expired) return { label: "已截止", tone: "neutral" };
  if (remaining.urgent) return { label: "临近截止", tone: "warn" };
  return { label: remaining.text, tone: "info" };
}

export function toTodayTaskVM(dto: DashboardTaskDto): TodayTaskVM {
  const remaining = formatRemaining(dto.due_at);
  const badge = badgeFor(dto, remaining);

  return {
    id: dto.id,
    assignmentId: dto.assignment_id,
    title: dto.title,
    courseName: dto.course_name,
    dueLabel: formatMonthDayTime(dto.due_at),
    remaining,
    badgeLabel: badge.label,
    badgeTone: badge.tone,
    href: `/courses/${dto.course_id}/assignments/${dto.assignment_id}`,
    actionLabel: dto.submitted ? "查看详情" : "继续完成",
  };
}
