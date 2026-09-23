/**
 * 课程资料接口（契约第 4、5 节）。
 *
 * 注意契约 4.7：`PROCESSING` + 任务 `PENDING` 表示「排队或处理中」，
 * 不是失败；只有 `FAILED` 才是失败。资料状态以 `MaterialDetail.status` 为准，
 * 大纲接口按它分流（READY → 200 / PROCESSING → 409 / FAILED → 502）。
 */

import { buildQuery, http } from "@/services/http";
import type { JobStatusDto } from "@/services/jobs";
import type { Page, Schemas } from "@/types/api";

export type MaterialDetailDto = Schemas["MaterialDetail"];
export type MaterialDownloadUrlDto = Schemas["MaterialDownloadUrl"];
export type MaterialOutlineDto = Schemas["MaterialOutline"];
export type MaterialSectionDto = Schemas["MaterialSection"];
export type MaterialKnowledgePointDto = Schemas["MaterialKnowledgePoint"];
export type MaterialStatusDto = Schemas["MaterialStatus"];
export type MaterialSourceTypeDto = Schemas["MaterialSectionSourceType"];
export type MaterialUploadInitRequestDto = Schemas["MaterialUploadInitRequest"];
export type MaterialUploadInitResponseDto = Schemas["MaterialUploadInitResponse"];
export type MaterialUploadCompleteResponseDto = Schemas["MaterialUploadCompleteResponse"];

export const materialsApi = {
  /** 契约 5.1：GET /courses/{course_id}/materials —— 成员可读，含归档课程 */
  list(courseId: string, page = 1, pageSize = 100): Promise<Page<MaterialDetailDto>> {
    return http.get<Page<MaterialDetailDto>>(
      `/courses/${courseId}/materials${buildQuery({ page, page_size: pageSize })}`,
    );
  },

  /** 契约 4.7：GET /materials/{material_id} */
  detail(materialId: string, signal?: AbortSignal): Promise<MaterialDetailDto> {
    return http.get<MaterialDetailDto>(`/materials/${materialId}`, { signal });
  },

  /** 契约 5.4：GET /materials/{material_id}/outline */
  outline(materialId: string, signal?: AbortSignal): Promise<MaterialOutlineDto> {
    return http.get<MaterialOutlineDto>(`/materials/${materialId}/outline`, { signal });
  },

  /**
   * 契约 4.9：GET /materials/{material_id}/download-url —— 资料原文的下载地址。
   *
   * 返回预签名 GET，有效期默认 10 分钟。签名是服务端**纯本地计算**的，
   * 因此每次请求都会拿到新的有效地址，前端不需要自己推算过期时间。
   */
  downloadUrl(
    materialId: string,
    signal?: AbortSignal,
  ): Promise<MaterialDownloadUrlDto> {
    return http.get<MaterialDownloadUrlDto>(
      `/materials/${materialId}/download-url`,
      { signal },
    );
  },

  /** 契约 4.3：POST /courses/{course_id}/materials/uploads */
  initUpload(
    courseId: string,
    body: MaterialUploadInitRequestDto,
  ): Promise<MaterialUploadInitResponseDto> {
    return http.post<MaterialUploadInitResponseDto>(
      `/courses/${courseId}/materials/uploads`,
      body,
    );
  },

  /**
   * 契约 4.5：完成上传。
   * 接口没有请求字段；重复完成是幂等的（202，回填首次快照）。
   */
  completeUpload(
    courseId: string,
    uploadId: string,
  ): Promise<MaterialUploadCompleteResponseDto> {
    return http.post<MaterialUploadCompleteResponseDto>(
      `/courses/${courseId}/materials/uploads/${uploadId}/complete`,
      {},
    );
  },

  /** 契约 5.2：DELETE /materials/{material_id} —— 仅创建教师，幂等 */
  remove(materialId: string): Promise<void> {
    return http.delete<void>(`/materials/${materialId}`);
  },

  /**
   * 契约 5.3：POST /materials/{material_id}/parse —— 重试解析。
   * 复用原任务 ID；READY 时返回 409 MATERIAL_ALREADY_READY。
   */
  retryParse(materialId: string): Promise<JobStatusDto> {
    return http.post<JobStatusDto>(`/materials/${materialId}/parse`, {});
  },
};
