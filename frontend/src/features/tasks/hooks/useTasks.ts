/**
 * 「任务」页的数据装配：按课程聚合作业。
 *
 * 为什么是 N 个请求：契约里只有 `GET /courses/{course_id}/assignments`，
 * 没有跨课程任务接口；Dashboard 的 `pending_assignments` 固定最多 5 条、
 * 不分页也不支持筛选，撑不起一个可筛选的任务列表。
 * 因此这里与首页「今日待办」用同一条路：读课程 → 逐课程读作业 → 内存聚合。
 *
 * 用的 query key 与 `useAssignments` / `useDashboard` **完全相同**，
 * 所以从首页切到任务页不会重复请求，发布/关闭作业后的失效也是同一套。
 */

import { useQueries } from "@tanstack/react-query";

import { assignmentsApi } from "@/features/assignments/api";
import { toAssignmentVM } from "@/features/assignments/model/types";
import { useCourses } from "@/features/courses/hooks/useCourses";
import { queryKeys } from "@/services/queryKeys";

import { sortTasks, toTaskRowVM, type TaskRole, type TaskRowVM } from "../model/types";

export type UseTasksResult = {
  tasks: TaskRowVM[];
  isPending: boolean;
  isError: boolean;
  error: unknown;
  refetch: () => void;
};

export function useTasks(role: TaskRole): UseTasksResult {
  const coursesQuery = useCourses();
  const allCourses = coursesQuery.data ?? [];

  // 教师只看自己创建的课程：别人课里的作业不是他的"任务"
  const courses =
    role === "teacher" ? allCourses.filter((course) => course.isOwner) : allCourses;

  const assignmentQueries = useQueries({
    queries: courses.map((course) => ({
      queryKey: queryKeys.assignments(course.id),
      queryFn: () => assignmentsApi.list(course.id),
    })),
  });

  const rows = courses.flatMap((course, index) => {
    const items = assignmentQueries[index]?.data?.items ?? [];
    return items.map((dto) => toTaskRowVM(toAssignmentVM(dto), course.name, role));
  });

  const isPending =
    coursesQuery.isPending || assignmentQueries.some((result) => result.isPending);
  const firstFailed = assignmentQueries.find((result) => result.isError);
  const isError = coursesQuery.isError || Boolean(firstFailed);

  const refetch = () => {
    void coursesQuery.refetch();
    for (const result of assignmentQueries) {
      void result.refetch();
    }
  };

  return {
    tasks: sortTasks(rows),
    isPending,
    isError,
    error: coursesQuery.error ?? firstFailed?.error ?? null,
    refetch,
  };
}
