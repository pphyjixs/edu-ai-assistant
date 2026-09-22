/** 实验任务的查询与写操作 Hook。 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { queryKeys } from "@/services/queryKeys";

import {
  assignmentsApi,
  type AssignmentCreateRequestDto,
  type AssignmentUpdateRequestDto,
} from "../api";
import { toAssignmentVM } from "../model/types";

export function useAssignments(courseId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.assignments(courseId ?? "none"),
    queryFn: () => assignmentsApi.list(courseId as string),
    select: (page) => ({
      total: page.total,
      items: page.items.map(toAssignmentVM),
    }),
    enabled: Boolean(courseId),
  });
}

export function useAssignment(assignmentId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.assignment(assignmentId ?? "none"),
    queryFn: ({ signal }) => assignmentsApi.detail(assignmentId as string, signal),
    select: toAssignmentVM,
    enabled: Boolean(assignmentId),
    // 学生视角下草稿返回 404，重试没有意义
    retry: false,
  });
}

/* ------------------------------- 写操作 ------------------------------- */

function useInvalidateAssignments(courseId: string, assignmentId?: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.assignments(courseId) });
    if (assignmentId) {
      void queryClient.invalidateQueries({ queryKey: queryKeys.assignment(assignmentId) });
    }
  };
}

export function useCreateAssignment(courseId: string) {
  const invalidate = useInvalidateAssignments(courseId);

  return useMutation({
    mutationFn: (body: AssignmentCreateRequestDto) => assignmentsApi.create(courseId, body),
    onSuccess: () => invalidate(),
  });
}

export function useUpdateAssignment(courseId: string, assignmentId: string) {
  const invalidate = useInvalidateAssignments(courseId, assignmentId);

  return useMutation({
    mutationFn: (body: AssignmentUpdateRequestDto) =>
      assignmentsApi.update(assignmentId, body),
    onSuccess: () => invalidate(),
  });
}

export function usePublishAssignment(courseId: string, assignmentId: string) {
  const invalidate = useInvalidateAssignments(courseId, assignmentId);

  return useMutation({
    mutationFn: () => assignmentsApi.publish(assignmentId),
    onSuccess: () => invalidate(),
  });
}

export function useCloseAssignment(courseId: string, assignmentId: string) {
  const invalidate = useInvalidateAssignments(courseId, assignmentId);

  return useMutation({
    mutationFn: () => assignmentsApi.close(assignmentId),
    onSuccess: () => invalidate(),
  });
}
