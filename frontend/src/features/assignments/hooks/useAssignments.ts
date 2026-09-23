/** 实验任务的查询与写操作 Hook。 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { queryKeys } from "@/services/queryKeys";

import {
  assignmentsApi,
  type AssignmentCreateRequestDto,
  type AssignmentUpdateRequestDto,
} from "../api";
import { toAssignmentVM } from "../model/types";
import { toAttachmentVM, uploadAttachment, type UploadStep } from "../model/attachment";

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

/**
 * 重新开启任务（契约 8.16）。CLOSED→PUBLISHED；已发布时幂等。
 *
 * 开启后学生可以继续提交，因此把提交列表也一并失效，避免教师看到旧的统计。
 */
export function useReopenAssignment(courseId: string, assignmentId: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: () => assignmentsApi.reopen(assignmentId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.assignments(courseId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.assignment(assignmentId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.submissions(assignmentId) });
    },
  });
}

/* ------------------------------- 附件 ------------------------------- */

/** 任务附件列表（契约 8.15）。地址短时有效，因此每次进入页面都重新取。 */
export function useAssignmentAttachments(assignmentId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.assignmentAttachments(assignmentId ?? "none"),
    queryFn: ({ signal }) => assignmentsApi.attachments(assignmentId as string, signal),
    select: (items) => items.map(toAttachmentVM),
    enabled: Boolean(assignmentId),
    staleTime: 0,
  });
}

/**
 * 上传附件（仅课程创建教师）。
 *
 * 成功与失败都会刷新列表：同名附件冲突（409 ATTACHMENT_ALREADY_EXISTS）
 * 时列表里已经有那条旧附件，教师可以直接在那里删除。
 */
export function useUploadAssignmentAttachment(assignmentId: string) {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<UploadStep | null>(null);

  const mutation = useMutation({
    mutationFn: (file: File) =>
      uploadAttachment({ assignmentId, file, onStep: setStep }),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.assignmentAttachments(assignmentId),
      });
    },
    onSettled: () => setStep(null),
  });

  return { ...mutation, step };
}

/** 删除附件（仅课程创建教师）。 */
export function useDeleteAssignmentAttachment(assignmentId: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (attachmentId: string) =>
      assignmentsApi.deleteAttachment(assignmentId, attachmentId),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.assignmentAttachments(assignmentId),
      });
    },
  });
}
