/**
 * 契约类型的集中出口。
 *
 * 所有 DTO 都来自 ``contracts/generated/api-types.ts``（由 OpenAPI 生成，
 * 禁止人工编辑，见 docs/collaboration.md 第 4 节）。前端不再手写一份
 * 与后端重复的 DTO；feature 的 ``api/`` 层负责「DTO → ViewModel」的转换。
 */

import type { components, paths } from "@contracts/api-types";

/** 契约里的具名 Schema，例如 Schemas["CourseSummary"] */
export type Schemas = components["schemas"];

/** 契约里的路径表，便于按 operationId 取请求/响应类型 */
export type ApiPaths = paths;

/**
 * 分页信封。
 *
 * 形状直接派生自契约里已生成的 ``Page[CourseSummary]``，只把 ``items``
 * 换成泛型——不重新声明 page / page_size / total，避免与后端脱节。
 */
export type Page<T> = Omit<Schemas["Page_CourseSummary_"], "items"> & { items: T[] };

/** 契约第 1 节的统一错误体（正常路径用不到，供调试与类型收窄） */
export type ApiErrorResponse = Schemas["ErrorResponse"];
export type ApiErrorBody = Schemas["ErrorBody"];
