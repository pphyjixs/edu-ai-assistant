/**
 * 通用的展示格式化。
 *
 * 只放**跨 feature 复用**的纯函数：资料/提交都在展示文件大小，
 * 因此这里集中一份，避免每个 feature 各抄一遍除法。
 */

/** 字节数 → 「512 B」「34 KB」「1.2 MB」 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * 分数展示：去掉无意义的小数尾巴（40 → 40，40.5 → 40.5）。
 *
 * 后端用 `Decimal` 精确计算、最多两位小数，前端只做展示，
 * 不参与任何分数运算（总分由服务端按分项求和）。
 */
export function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

/** 0–1 的完成比例 → 百分比整数 */
export function formatPercent(ratio: number): string {
  if (!Number.isFinite(ratio)) return "—";
  return `${Math.round(Math.max(0, Math.min(1, ratio)) * 100)}%`;
}
