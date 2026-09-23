/**
 * 提交相关查询与写操作。
 *
 * 分页语义（契约 9.4）：教师拿到全班正式提交，学生拿到本人 0–1 条；
 * 未完成的 `UPLOADING` 记录**不会**出现在列表里，因此学生页面不能靠列表
 * 判断"我有没有开始上传"，只能看本地上传状态或直接看详情。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { queryKeys } from "@/services/queryKeys";

import { gradingApi, SUBMISSION_PAGE_SIZE } from "../api";
import { toSubmissionVM } from "../model/types";
import { uploadSubmission, type UploadStep } from "../model/upload";

/** 某作业的提交列表；`isTeacher` 决定状态文案（待批改 vs 已提交） */
export function useSubmissions(
  assignmentId: string | undefined,
  options: { isTeacher: boolean; page?: number } = { isTeacher: false },
) {
  const page = options.page ?? 1;

  return useQuery({
    queryKey: [...queryKeys.submissions(assignmentId ?? "none"), page] as const,
    queryFn: ({ signal }) =>
      gradingApi.listSubmissions(assignmentId as string, page, SUBMISSION_PAGE_SIZE, signal),
    select: (data) => ({
      ...data,
      items: data.items.map((item) =>
        toSubmissionVM(item, { isTeacher: options.isTeacher }),
      ),
    }),
    enabled: Boolean(assignmentId),
    staleTime: 0,
  });
}

/**
 * 提交详情（含短时下载地址）。
 *
 * `pollWhileGrading`：批改期间每 3 秒重取一次。这样即使刷新页面丢了本地的
 * 任务 ID，状态也会从 `GRADING` 自动推进到 `REVIEW_REQUIRED`——
 * 与资料解析、练习生成的恢复方式一致。
 */
export function useSubmission(
  submissionId: string | undefined,
  options: { isTeacher: boolean; pollWhileGrading?: boolean } = { isTeacher: false },
) {
  return useQuery({
    queryKey: queryKeys.submission(submissionId ?? "none"),
    queryFn: ({ signal }) => gradingApi.submission(submissionId as string, signal),
    select: (dto) => toSubmissionVM(dto, { isTeacher: options.isTeacher }),
    enabled: Boolean(submissionId),
    staleTime: 0,
    refetchInterval: (query) =>
      options.pollWhileGrading && query.state.data?.status === "GRADING" ? 3_000 : false,
  });
}

/**
 * 上传并提交报告。
 *
 * 成功后要让三处刷新：该作业的提交列表、本次提交详情、以及作业本身
 * （学生界面的提交状态就挂在作业详情页上）。
 */
export function useUploadSubmission(assignmentId: string, courseId: string) {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<UploadStep | null>(null);

  const mutation = useMutation({
    mutationFn: (file: File) =>
      uploadSubmission({ assignmentId, file, onStep: setStep }),
    onSuccess: (submission) => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.submissions(assignmentId),
      });
      void queryClient.invalidateQueries({
        queryKey: queryKeys.submission(submission.id),
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.assignment(assignmentId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.assignments(courseId) });
    },
    onSettled: () => setStep(null),
  });

  return { ...mutation, step };
}

/**
 * 触发或重试 AI 批改（契约 9.6）。
 *
 * 返回的是 `JobStatus`，界面需要轮询到终态后再刷新批改详情与提交详情——
 * 模型调用是异步的，`202` 只表示任务已受理。
 */
export function useGradeSubmission(assignmentId: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (submissionId: string) => gradingApi.grade(submissionId),
    onSuccess: (_job, submissionId) => {
      // 提交立刻进入 GRADING，列表与详情都要跟着变
      void queryClient.invalidateQueries({ queryKey: queryKeys.submission(submissionId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.submissions(assignmentId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.gradeReview(submissionId) });
    },
  });
}
