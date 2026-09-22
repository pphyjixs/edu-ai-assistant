/** 资料查询与写操作 Hook。 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { queryKeys } from "@/services/queryKeys";

import { materialsApi } from "../api";
import { uploadMaterial, type UploadStep } from "../api/upload";
import { toMaterialOutlineVM, toMaterialVM } from "../model/types";

export function useMaterials(courseId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.materials(courseId ?? "none"),
    queryFn: () => materialsApi.list(courseId as string),
    select: (page) => ({
      total: page.total,
      items: page.items.map(toMaterialVM),
    }),
    enabled: Boolean(courseId),
  });
}

export function useMaterial(materialId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.material(materialId ?? "none"),
    queryFn: ({ signal }) => materialsApi.detail(materialId as string, signal),
    select: toMaterialVM,
    enabled: Boolean(materialId),
  });
}

/**
 * 资料大纲。
 *
 * 契约 5.4 按资料状态分流：`READY` 返回 200，`PROCESSING` 返回 409
 * `MATERIAL_NOT_READY`，`FAILED` 返回 502 `AI_JOB_FAILED`。
 * 这三种都不是「请求失败」，所以关掉重试，把错误码交给界面去分流展示。
 */
export function useMaterialOutline(materialId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.materialOutline(materialId ?? "none"),
    queryFn: ({ signal }) => materialsApi.outline(materialId as string, signal),
    select: toMaterialOutlineVM,
    enabled: Boolean(materialId),
    retry: false,
  });
}

/* ------------------------------- 上传 ------------------------------- */

export function useUploadMaterial(courseId: string) {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<UploadStep | null>(null);

  const mutation = useMutation({
    mutationFn: (file: File) => uploadMaterial({ courseId, file, onStep: setStep }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.materials(courseId) });
    },
    onSettled: () => setStep(null),
  });

  return { ...mutation, step };
}

/* ---------------------------- 删除 / 重试 ---------------------------- */

export function useDeleteMaterial(courseId: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (materialId: string) => materialsApi.remove(materialId),
    onSuccess: (_result, materialId) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.materials(courseId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.material(materialId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.materialOutline(materialId) });
    },
  });
}

export function useRetryParse(courseId: string, materialId: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: () => materialsApi.retryParse(materialId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.materials(courseId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.material(materialId) });
    },
  });
}
