/** 拼接 CSS Module 类名，过滤掉 undefined / false */
export function cn(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(" ");
}
