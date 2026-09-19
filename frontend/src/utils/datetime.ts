/**
 * 时间格式化。
 *
 * 契约规定后端以 ISO 8601 UTC 返回时间（docs/api-contract.md 第 1 节），
 * 前端统一按用户本地时区展示。这个模块是唯一允许做时间格式化的地方，
 * 避免各 feature 各写一套。
 *
 * 注：DEVELOPMENT_SPEC 的目录清单里没有 utils/，但时间格式化被
 * assignments、dashboard、materials 三个模块共用，集中一处比复制三次好。
 */

const MONTH_DAY_TIME: Intl.DateTimeFormatOptions = {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
};

function toDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** 「09-20 23:59」，用于卡片底部 */
export function formatMonthDayTime(iso: string | null | undefined): string {
  const date = toDate(iso);
  if (!date) return "未设置";
  const parts = new Intl.DateTimeFormat("zh-CN", MONTH_DAY_TIME).formatToParts(date);
  const read = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((part) => part.type === type)?.value ?? "";
  return `${read("month")}-${read("day")} ${read("hour")}:${read("minute")}`;
}

/** 「09 月 20 日 23:59」，用于详情页的截止卡片 */
export function formatDeadline(iso: string | null | undefined): string {
  const date = toDate(iso);
  if (!date) return "未设置";
  const parts = new Intl.DateTimeFormat("zh-CN", MONTH_DAY_TIME).formatToParts(date);
  const read = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((part) => part.type === type)?.value ?? "";
  return `${read("month")} 月 ${read("day")} 日 ${read("hour")}:${read("minute")}`;
}

export type Remaining = {
  text: string;
  /** 用于「临近截止」的橙色标签，不使用大面积红色告警 */
  urgent: boolean;
  expired: boolean;
};

/**
 * 距截止时间的可读剩余量。
 * 只根据真实的 due_at 计算，后端没有 due_at 时返回「未设置截止时间」。
 *
 * 用四舍五入而不是向下取整：时间差是浮点结果，差一点不到 24 小时的语料
 * 应该显示「剩余 1 天」，而不是别扭的「剩余 24 小时」。
 */
export function formatRemaining(iso: string | null | undefined, now = new Date()): Remaining {
  const date = toDate(iso);
  if (!date) return { text: "未设置截止时间", urgent: false, expired: false };

  const diffMs = date.getTime() - now.getTime();
  if (diffMs <= 0) return { text: "已截止", urgent: false, expired: true };

  const hours = diffMs / (1000 * 60 * 60);

  if (hours < 1) {
    const minutes = Math.max(1, Math.round(diffMs / (1000 * 60)));
    return { text: `剩余 ${minutes} 分钟`, urgent: true, expired: false };
  }

  if (hours < 22) {
    return { text: `剩余 ${Math.round(hours)} 小时`, urgent: true, expired: false };
  }

  const days = Math.max(1, Math.round(hours / 24));
  return { text: `剩余 ${days} 天`, urgent: days <= 3, expired: false };
}

/** 首页问候语 */
export function greetingFor(now = new Date()): string {
  const hour = now.getHours();
  if (hour < 5) return "凌晨好";
  if (hour < 11) return "早上好";
  if (hour < 14) return "中午好";
  if (hour < 18) return "下午好";
  return "晚上好";
}

/** 「SATURDAY · SEPTEMBER 19」样式的日期眉题 */
export function formatEyebrowDate(now = new Date()): string {
  return new Intl.DateTimeFormat("en-US", {
    weekday: "long",
    month: "long",
    day: "numeric",
  })
    .format(now)
    .toUpperCase();
}
