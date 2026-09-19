/**
 * 课程的前端视图模型。
 *
 * DTO → VM 的转换集中在 model 层：页面和组件只认 VM，
 * 因此后端枚举改名（例如 ACTIVE → PUBLISHED）时只需要改这里。
 */

import type { IllustrationName } from "@/components/EmptyState/EmptyState";

import type { CourseDto } from "../api";

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
  teacherName: string;
  status: "active" | "archived";
  statusLabel: string;
  accent: CourseAccent;
  cover: IllustrationName;
};

export function toCourseVM(dto: CourseDto): CourseVM {
  const accent = accentForCourse(dto.id);
  return {
    id: dto.id,
    name: dto.name,
    description: dto.description,
    teacherName: dto.teacher.display_name,
    status: dto.status === "ARCHIVED" ? "archived" : "active",
    statusLabel: dto.status === "ARCHIVED" ? "已归档" : "进行中",
    accent,
    cover: COVER_BY_ACCENT[accent],
  };
}
