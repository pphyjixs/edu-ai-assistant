/** 作业查询与提交 Hook。 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { queryKeys } from "@/services/queryKeys";

import { assignmentsApi, type CreateSubmissionBody } from "../api";
import {
  toAssignmentVM,
  toSubmissionVM,
  type AssignmentVM,
  type SubmissionVM,
} from "../model/types";

export function useAssignments(courseId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.assignments(courseId ?? "none"),
    queryFn: () => assignmentsApi.listAssignments(courseId as string),
    select: (items): AssignmentVM[] => items.map(toAssignmentVM),
    enabled: Boolean(courseId),
  });
}

/** 作业详情。``assignmentId`` 允许为空，便于 Buddy 上下文条按需调用。 */
export function useAssignment(assignmentId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.assignment(assignmentId ?? "none"),
    queryFn: () => assignmentsApi.getAssignment(assignmentId as string),
    select: toAssignmentVM,
    enabled: Boolean(assignmentId),
  });
}

export function useMySubmission(assignmentId: string | undefined) {
  return useQuery({
    queryKey: [...queryKeys.assignment(assignmentId ?? "none"), "my-submission"],
    queryFn: () => assignmentsApi.getMySubmission(assignmentId as string),
    select: (dto): SubmissionVM | null => (dto ? toSubmissionVM(dto) : null),
    enabled: Boolean(assignmentId),
  });
}

export function useCreateSubmission(assignmentId: string | undefined) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: CreateSubmissionBody) =>
      assignmentsApi.createSubmission(assignmentId as string, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.assignment(assignmentId ?? "none"),
      });
    },
  });
}
