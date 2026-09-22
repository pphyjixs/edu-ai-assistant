/**
 * 首页数据。
 *
 * 契约第 11 节的 Dashboard 聚合接口**尚未实现**，因此这里不调不存在的接口，
 * 也不使用示例数据，而是复用已经是真实接口的两个查询结果在内存里组合：
 *
 *   GET /courses                      → 我参加的课程
 *   GET /courses/{course_id}/assignments → 各课程的任务（学生视角只含已发布）
 *
 * 这样首页显示的每一个数字与卡片都能对应到数据库里的真实记录；
 * 等 Dashboard 接口落地后，把这个 hook 换成单个请求即可，页面无需改动。
 */

import { useQueries } from "@tanstack/react-query";

import { assignmentsApi } from "@/features/assignments/api";
import { toAssignmentVM, type AssignmentVM } from "@/features/assignments/model/types";
import { useCourses } from "@/features/courses/hooks/useCourses";
import { queryKeys } from "@/services/queryKeys";

import {
  toTodayTaskVM,
  type StudentSummaryVM,
  type TeacherSummaryVM,
} from "../model/types";

const MAX_TASKS = 4;

type CourseWithAssignments = {
  courseId: string;
  courseName: string;
  isOwner: boolean;
  isArchived: boolean;
  assignments: AssignmentVM[];
};

function sortForStudent(a: AssignmentVM, b: AssignmentVM): number {
  // 已关闭的排最后；其余按截止时间升序，没有截止时间的排在有截止时间的后面
  if (a.status === "closed" && b.status !== "closed") return 1;
  if (b.status === "closed" && a.status !== "closed") return -1;
  if (a.dueAt === null && b.dueAt !== null) return 1;
  if (b.dueAt === null && a.dueAt !== null) return -1;
  if (a.dueAt && b.dueAt) return a.dueAt.localeCompare(b.dueAt);
  return 0;
}

function sortForTeacher(a: AssignmentVM, b: AssignmentVM): number {
  // 待发布的草稿优先，其余按截止时间
  if (a.status === "draft" && b.status !== "draft") return -1;
  if (b.status === "draft" && a.status !== "draft") return 1;
  return sortForStudent(a, b);
}

export function useDashboard(role: "teacher" | "student") {
  const coursesQuery = useCourses();
  const courses = coursesQuery.data ?? [];

  const assignmentQueries = useQueries({
    queries: courses.map((course) => ({
      queryKey: queryKeys.assignments(course.id),
      queryFn: () => assignmentsApi.list(course.id),
    })),
  });

  const isPending =
    coursesQuery.isPending || assignmentQueries.some((result) => result.isPending);

  const perCourse: CourseWithAssignments[] = courses.map((course, index) => ({
    courseId: course.id,
    courseName: course.name,
    isOwner: course.isOwner,
    isArchived: course.status === "archived",
    assignments: (assignmentQueries[index]?.data?.items ?? []).map(toAssignmentVM),
  }));

  /** 教师视角只关心自己创建的课程 */
  const scope = role === "teacher" ? perCourse.filter((course) => course.isOwner) : perCourse;

  const flatten = scope.flatMap((course) =>
    course.assignments.map((assignment) => ({ course, assignment })),
  );

  const isLikelyUnavailable = assignmentQueries.some((result) => result.isError);
  const firstError = coursesQuery.error ?? assignmentQueries.find((r) => r.isError)?.error ?? null;

  /** 首页的「重试」需要把课程与各课程任务一起刷新 */
  const refetch = async () => {
    await Promise.all([
      coursesQuery.refetch(),
      ...assignmentQueries.map((result) => result.refetch()),
    ]);
  };

  if (role === "teacher") {
    const drafts = flatten.filter((item) => item.assignment.status === "draft");
    const published = flatten.filter((item) => item.assignment.status === "published");

    const summary: TeacherSummaryVM = {
      activeCourseCount: scope.filter((course) => !course.isArchived).length,
      draftCount: drafts.length,
      tasks: [...drafts, ...published]
        .sort((a, b) => sortForTeacher(a.assignment, b.assignment))
        .slice(0, MAX_TASKS)
        .map((item) => toTodayTaskVM(item.assignment, item.course.courseName)),
    };

    return {
      role: "teacher" as const,
      isPending,
      isError: isLikelyUnavailable,
      error: firstError,
      refetch,
      summary,
    };
  }

  const studentAssignments = flatten.filter((item) => item.assignment.status !== "archived");
  const summary: StudentSummaryVM = {
    pendingCount: studentAssignments.filter((item) => item.assignment.canSubmit).length,
    closedCount: studentAssignments.filter((item) => item.assignment.status === "closed").length,
    tasks: [...studentAssignments]
      .sort((a, b) => sortForStudent(a.assignment, b.assignment))
      .slice(0, MAX_TASKS)
      .map((item) => toTodayTaskVM(item.assignment, item.course.courseName)),
  };

  return {
    role: "student" as const,
    isPending,
    isError: isLikelyUnavailable,
    error: firstError,
    refetch,
    summary,
  };
}
