/**
 * 课程的视图模型。
 *
 * DTO → VM 的转换点：页面与组件只认 VM，后端枚举改名时只需要改这里。
 */

import type { IllustrationName } from "@/components/EmptyState/EmptyState";
import { formatMonthDayTime } from "@/utils/datetime";

import type { CourseDetailResponseDto, CourseMemberSummaryDto, CourseSummaryDto } from "../api";

export type CourseAccent = "blue" | "purple" | "green";

const ACCENTS: CourseAccent[] = ["blue", "purple", "green"];

const COVER_BY_ACCENT: Record<CourseAccent, IllustrationName> = {
  blue: "course-ai",
  purple: "course-db",
  green: "course-system",
};

/**
 * 课程封面色。纯装饰，用 id 做稳定散列，
 * 保证同一门课每次渲染拿到同一个颜色，不随数组顺序变化。
 */
export function accentForCourse(courseId: string): CourseAccent {
  let hash = 0;
  for (const char of courseId) {
    hash = (hash * 31 + char.charCodeAt(0)) % 100000;
  }
  return ACCENTS[hash % ACCENTS.length];
}

export function coverForCourse(courseId: string): IllustrationName {
  return COVER_BY_ACCENT[accentForCourse(courseId)];
}

export type CourseVM = {
  id: string;
  name: string;
  description: string;
  status: "active" | "archived";
  statusLabel: string;
  /**
   * 当前用户是否为创建教师。
   *
   * 契约 3.1：邀请码只有创建教师能重置，创建和修改课程的响应也证明
   * 只有创建教师能写；而 `/courses/join` 仅限学生调用。
   * 因此「非创建教师的成员」必然是学生——这条推断的边界就在这里。
   */
  isOwner: boolean;
  /** 仅在创建教师查看未归档课程时存在 */
  inviteCode: string | null;
  accent: CourseAccent;
  cover: IllustrationName;
  /** 最近更新时间的可读形式 */
  updatedAtLabel: string;
};

function baseVM(dto: CourseSummaryDto, currentUserId: string | undefined) {
  const accent = accentForCourse(dto.id);
  return {
    id: dto.id,
    name: dto.name,
    description: dto.description,
    status: dto.status === "ARCHIVED" ? ("archived" as const) : ("active" as const),
    statusLabel: dto.status === "ARCHIVED" ? "已归档" : "进行中",
    isOwner: currentUserId !== undefined && dto.teacher_id === currentUserId,
    accent,
    cover: COVER_BY_ACCENT[accent],
    updatedAtLabel: formatMonthDayTime(dto.updated_at),
  };
}

export function toCourseVM(dto: CourseSummaryDto, currentUserId: string | undefined): CourseVM {
  return {
    ...baseVM(dto, currentUserId),
    inviteCode: null,
  };
}

export function toCourseDetailVM(
  dto: CourseDetailResponseDto,
  currentUserId: string | undefined,
): CourseVM {
  const inviteCode =
    "invite_code" in dto && typeof dto.invite_code === "string" ? dto.invite_code : null;
  return {
    ...baseVM(dto, currentUserId),
    inviteCode,
  };
}

export type CourseMemberVM = {
  userId: string;
  displayName: string;
  roleLabel: string;
  initial: string;
};

export function toCourseMemberVM(dto: CourseMemberSummaryDto): CourseMemberVM {
  return {
    userId: dto.user_id,
    displayName: dto.display_name,
    roleLabel: dto.course_role === "TEACHER" ? "教师" : "学生",
    initial: dto.display_name.slice(0, 1).toUpperCase(),
  };
}
