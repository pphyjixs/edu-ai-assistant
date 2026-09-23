/**
 * 批改详情、教师复核与发布。
 *
 * 批改详情的三种"非成功"结果是**正常状态**，不是请求失败（契约 9.7）：
 *
 * - `409 SUBMISSION_NOT_READY`：批改还没生成（`GRADING` / `FAILED`），教师看到「还在批改」；
 * - `404 RESOURCE_NOT_FOUND`：学生视角在成绩发布前读不到，页面要显示「等教师发布」；
 * - `502 AI_JOB_FAILED`：批改任务失败，`details.job_id` 指向任务。
 *
 * 因此这里关掉自动重试，把错误码交给页面分流，而不是统一弹一个「加载失败」。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { queryKeys } from "@/services/queryKeys";

import { gradingApi, type GradeReviewUpdateRequestDto } from "../api";
import { toGradeReviewVM } from "../model/types";

/** 批改详情；`enabled` 由页面决定（学生只在已发布时请求） */
export function useGradeReview(
  submissionId: string | undefined,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: queryKeys.gradeReview(submissionId ?? "none"),
    queryFn: ({ signal }) => gradingApi.gradeReview(submissionId as string, signal),
    select: toGradeReviewVM,
    enabled: Boolean(submissionId) && (options.enabled ?? true),
    staleTime: 0,
    retry: false,
  });
}

/**
 * 教师复核（契约 9.8）。
 *
 * 请求体是**完整快照**：必须恰好覆盖该提交引用版本的全部评分项，
 * 因此调用方要把界面上所有评分项一起提交，而不是只发被改过的那几项。
 */
export function useUpdateGradeReview(submissionId: string, assignmentId: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (params: { reviewId: string; body: GradeReviewUpdateRequestDto }) =>
      gradingApi.updateGradeReview(params.reviewId, params.body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.gradeReview(submissionId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.submission(submissionId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.submissions(assignmentId) });
    },
  });
}

/**
 * 发布成绩（契约 9.9）。
 *
 * 未复核时后端返回 `409 GRADE_NOT_REVIEWED`；重复发布是幂等的。
 */
export function usePublishGradeReview(submissionId: string, assignmentId: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (reviewId: string) => gradingApi.publishGradeReview(reviewId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.gradeReview(submissionId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.submission(submissionId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.submissions(assignmentId) });
      // 学生的成绩页按作业逐条查提交与批改，这里让整组失效
      void queryClient.invalidateQueries({ queryKey: ["submissions"] });
      void queryClient.invalidateQueries({ queryKey: ["grade-review"] });
    },
  });
}
