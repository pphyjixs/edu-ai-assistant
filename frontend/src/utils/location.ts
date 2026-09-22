/**
 * 来源定位的格式化。
 *
 * 契约里位置有三种来源（``MaterialSectionSourceType``）：PDF 页码、
 * PPTX 幻灯片号、DOCX 段落序号，都从 1 开始。资料大纲（5.4）与
 * 问答引用（6.6）都要展示它，因此集中一处，避免两套写法。
 *
 * 页面与引用共用这个模块；没有可用定位时返回空字符串，由调用方决定不显示，
 * 绝不补一个猜的页码。
 */

const LOCATION_UNIT: Record<string, string> = {
  PDF_PAGE: "P",
  PPTX_SLIDE: "幻灯片",
  DOCX_PARAGRAPH: "段落",
};

export const SOURCE_TYPE_LABEL: Record<string, string> = {
  PDF_PAGE: "PDF 页码",
  PPTX_SLIDE: "幻灯片",
  DOCX_PARAGRAPH: "段落",
};

/**
 * 生成「P3」「幻灯片 5」「段落 12-14」这样的定位文案。
 *
 * @param sourceType 契约的 source_type
 * @param start 起始位置（从 1 开始）
 * @param end 结束位置，未提供时与 start 相同
 * @param page 引用里额外的 page 字段，优先使用
 */
export function formatLocation(
  sourceType: string | null | undefined,
  start: number | null | undefined,
  end?: number | null,
  page?: number | null,
): string {
  if (typeof page === "number" && page > 0) return `P${page}`;

  if (!sourceType || typeof start !== "number" || start < 1) return "";

  const unit = LOCATION_UNIT[sourceType];
  if (!unit) return "";

  const finish = typeof end === "number" && end > start ? end : start;
  return finish === start ? `${unit}${start}` : `${unit}${start}-${finish}`;
}
