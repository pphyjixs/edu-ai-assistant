/** 课程查询 Hook。页面只依赖这些 Hook，不直接接触 API 函数。 */

import { useQuery } from "@tanstack/react-query";

import { queryKeys } from "@/services/queryKeys";

import { coursesApi } from "../api";
import { toCourseVM, type CourseVM } from "../model/types";

export function useCourses() {
  return useQuery({
    queryKey: queryKeys.courses,
    queryFn: () => coursesApi.listMyCourses(),
    select: (courses): CourseVM[] => courses.map(toCourseVM),
  });
}

/**
 * 课程详情。``courseId`` 允许为空——Buddy 的上下文条会在没有课程上下文时
 * 调用它，此时不发请求（enabled: false）。
 */
export function useCourse(courseId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.course(courseId ?? "none"),
    queryFn: () => coursesApi.getCourse(courseId as string),
    select: toCourseVM,
    enabled: Boolean(courseId),
  });
}
