/** 练习查询与写操作 Hook。 */

import { useMutation, useQuery, useQueries, useQueryClient } from "@tanstack/react-query";

import { queryKeys } from "@/services/queryKeys";
import { HttpError } from "@/services/http";

import { practiceApi, type PracticeGenerateRequestDto } from "../api";
import { attemptIndex } from "../model/attemptStore";
import { generatedPracticeIndex } from "../model/generatedStore";
import {
  toPracticeAttemptVM,
  toPracticeSetSummaryVM,
  toPracticeSetVM,
} from "../model/types";

export function usePracticeSets(courseId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.practiceSets(courseId ?? "none"),
    queryFn: () => practiceApi.list(courseId as string),
    select: (page) => ({
      total: page.total,
      items: page.items.map(toPracticeSetSummaryVM),
    }),
    enabled: Boolean(courseId),
  });
}

export function usePracticeSet(setId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.practiceSet(setId ?? "none"),
    queryFn: ({ signal }) => practiceApi.detail(setId as string, signal),
    select: toPracticeSetVM,
    enabled: Boolean(setId),
    // 学生视角下未发布的练习会返回 404，重试没有意义
    retry: false,
  });
}

/**
 * 答题结果。
 *
 * 需要练习详情来解释「选项 ID → 文字」，因此把详情一起取进来做映射；
 * 详情此时通常已在缓存里，不会产生额外请求。
 */
export function usePracticeAttempt(setId: string | undefined, attemptId: string | undefined) {
  const setQuery = usePracticeSet(setId);

  const query = useQuery({
    queryKey: queryKeys.practiceAttempt(attemptId ?? "none"),
    queryFn: ({ signal }) => practiceApi.attempt(attemptId as string, signal),
    enabled: Boolean(attemptId),
    retry: false,
  });

  return {
    isPending: query.isPending,
    isError: query.isError,
    error: query.error,
    refetch: query.refetch,
    data: query.data ? toPracticeAttemptVM(query.data, setQuery.data) : undefined,
  };
}

/** 本地记住的答题记录（刷新后仍能回到结果页） */
export function useStoredAttemptId(setId: string | undefined): string | undefined {
  if (!setId) return undefined;
  return attemptIndex.get(setId);
}

/**
 * 教师自己生成、但还没进入已发布列表的练习。
 *
 * 契约 7.3 的列表只返回 PUBLISHED，因此草稿只能靠本地索引逐个取详情。
 * 取不到的条目（例如被删除或不是自己生成的）会被忽略。
 */
export function useGeneratedDrafts(courseId: string | undefined) {
  const entries = courseId ? generatedPracticeIndex.list(courseId) : [];

  const results = useQueries({
    queries: entries.map((entry) => ({
      queryKey: queryKeys.practiceSet(entry.setId),
      queryFn: () => practiceApi.detail(entry.setId),
      retry: false,
    })),
  });

  const items = results
    .map((result, index) => {
      const entry = entries[index];
      if (!result.data || !entry) return null;
      const vm = toPracticeSetVM(result.data);
      return vm.status === "published" ? null : { vm, jobId: entry.jobId };
    })
    .filter((item): item is { vm: ReturnType<typeof toPracticeSetVM>; jobId: string } => item !== null);

  return {
    items,
    isPending: results.some((result) => result.isPending),
  };
}

/* ------------------------------- 写操作 ------------------------------- */

export function useGeneratePractice(courseId: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: PracticeGenerateRequestDto) => practiceApi.generate(courseId, body),
    onSuccess: (job) => {
      // 契约 7.3 的列表只返回已发布练习，草稿只能靠本地索引找回来
      generatedPracticeIndex.add(courseId, {
        setId: job.resource_id,
        jobId: job.id,
        createdAt: job.created_at,
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.practiceSets(courseId) });
    },
  });
}

export function usePublishPractice(courseId: string, setId: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: () => practiceApi.publish(setId),
    onSuccess: () => {
      // 发布后它会出现在正常列表里，本地草稿索引可以退休了
      generatedPracticeIndex.remove(courseId, setId);
      void queryClient.invalidateQueries({ queryKey: queryKeys.practiceSet(setId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.practiceSets(courseId) });
    },
  });
}

export function useSubmitAttempt(courseId: string, setId: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (answers: Array<{ question_id: string; answer: string | boolean }>) =>
      practiceApi.submit(setId, { answers }),
    onSuccess: (result) => {
      // 记住 attempt_id，学生刷新后还能回到结果
      attemptIndex.save(setId, result.id);
      void queryClient.invalidateQueries({ queryKey: queryKeys.practiceAttempt(result.id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.practiceSets(courseId) });
    },
    onError: (error) => {
      // 已经提交过（可能是在别的标签页里提交的）：刷新详情，避免一直停在答题态
      if (error instanceof HttpError && error.code === "PRACTICE_ALREADY_ATTEMPTED") {
        void queryClient.invalidateQueries({ queryKey: queryKeys.practiceSet(setId) });
      }
    },
  });
}
