/** 课程查询与写操作 Hook。页面只依赖这些 Hook，不直接接触 API 函数。 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useCurrentUser } from "@/features/auth/hooks/useCurrentUser";
import { queryKeys } from "@/services/queryKeys";

import {
  coursesApi,
  type CourseCreateRequestDto,
  type CourseUpdateRequestDto,
} from "../api";
import { toCourseDetailVM, toCourseMemberVM, toCourseVM, type CourseVM } from "../model/types";

/** 首页与侧栏只需要「最近的若干门课」 */
const OVERVIEW_PAGE_SIZE = 50;

/** 首页与侧栏只要一个课程数组（取前若干门） */
export function useCourses(pageSize = OVERVIEW_PAGE_SIZE) {
  const userQuery = useCurrentUser();
  const currentUserId = userQuery.data?.id;

  return useQuery({
    queryKey: [...queryKeys.courses, pageSize] as const,
    queryFn: () => coursesApi.list(1, pageSize),
    select: (page): CourseVM[] => page.items.map((dto) => toCourseVM(dto, currentUserId)),
    enabled: Boolean(currentUserId),
  });
}

/** 课程列表页需要总数与分页信息 */
export function useCoursesPaged(page = 1, pageSize = 20) {
  const userQuery = useCurrentUser();
  const currentUserId = userQuery.data?.id;

  return useQuery({
    queryKey: [...queryKeys.courses, "paged", page, pageSize] as const,
    queryFn: () => coursesApi.list(page, pageSize),
    select: (result) => ({
      total: result.total,
      page: result.page,
      pageSize: result.page_size,
      items: result.items.map((dto): CourseVM => toCourseVM(dto, currentUserId)),
    }),
    enabled: Boolean(currentUserId),
  });
}

/**
 * 课程详情。``courseId`` 允许为空——Buddy 的上下文条会在没有课程上下文时
 * 调用它，此时不发请求。
 */
export function useCourse(courseId: string | undefined) {
  const userQuery = useCurrentUser();
  const currentUserId = userQuery.data?.id;

  return useQuery({
    queryKey: queryKeys.course(courseId ?? "none"),
    queryFn: ({ signal }) => coursesApi.get(courseId as string, signal),
    select: (dto): CourseVM => toCourseDetailVM(dto, currentUserId),
    enabled: Boolean(courseId),
  });
}

export function useCourseMembers(courseId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.courseMembers(courseId ?? "none"),
    queryFn: () => coursesApi.members(courseId as string, 1, 100),
    select: (page) => ({
      total: page.total,
      items: page.items.map(toCourseMemberVM),
    }),
    enabled: Boolean(courseId),
  });
}

/* ------------------------------- 写操作 ------------------------------- */

export function useCreateCourse() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: CourseCreateRequestDto) => coursesApi.create(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.courses });
    },
  });
}

export function useUpdateCourse(courseId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: CourseUpdateRequestDto) => coursesApi.update(courseId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.course(courseId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.courses });
    },
  });
}

export function useArchiveCourse(courseId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => coursesApi.archive(courseId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.course(courseId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.courses });
    },
  });
}

export function useRegenerateInviteCode(courseId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => coursesApi.regenerateInviteCode(courseId),
    onSuccess: () => {
      // 契约 3.4：客户端应从课程详情重新取得当前邀请码，不依赖本地缓存的旧码
      void queryClient.invalidateQueries({ queryKey: queryKeys.course(courseId) });
    },
  });
}

export function useJoinCourse() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (inviteCode: string) => coursesApi.join(inviteCode),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.courses });
    },
  });
}
