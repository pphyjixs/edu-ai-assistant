/**
 * 统一的 HTTP 边界。
 *
 * 契约依据：docs/api-contract.md 第 1、2 节。
 * - 基础路径由 ``VITE_API_BASE_URL`` 提供，形如 ``http://localhost:8000/api/v1``
 * - 认证头 ``Authorization: Bearer <access_token>``
 * - 错误统一为 ``{ error: { code, message, details, request_id } }``
 * - 401 使用 AUTH_TOKEN_EXPIRED；Access Token 1 小时，Refresh Token 7 天不轮换
 *
 * 页面与组件不得直接使用 fetch（docs/architecture.md 第 5 节），一律经这里。
 */

import type { Schemas } from "@/types/api";

/**
 * 契约第 12 节的稳定错误码全集。
 *
 * 直接取自生成的契约类型（`Schemas["ErrorCode"]`），不再手抄一份：
 * 手抄的列表会在后端新增错误码时悄悄落后——例如 `AGENT_*` 系列
 * 就没有同步过来，导致前端无法按键判断分支。
 * 只有 `NETWORK_ERROR` 是前端本地状态（请求没到达服务端），契约里不存在。
 */
export type ApiErrorCode = Schemas["ErrorCode"] | "NETWORK_ERROR";

export type AppError = {
  code: ApiErrorCode;
  /** 面向用户的可读文案；后端 message 缺失时由本层兜底 */
  message: string;
  status?: number;
  requestId?: string;
  /** 后端 details，例如 UPLOAD_INVALID 的 reason、限流的 retry_after_seconds */
  details: Record<string, unknown>;
  /** 由调用方决定是否展示重试入口 */
  retryable: boolean;
};

export class HttpError extends Error {
  readonly code: ApiErrorCode;
  readonly status: number;
  readonly requestId?: string;
  readonly details: Record<string, unknown>;

  constructor(params: {
    code: ApiErrorCode;
    message: string;
    status: number;
    requestId?: string;
    details?: Record<string, unknown>;
  }) {
    super(params.message);
    this.name = "HttpError";
    this.code = params.code;
    this.status = params.status;
    this.requestId = params.requestId;
    this.details = params.details ?? {};
  }

  toAppError(): AppError {
    return {
      code: this.code,
      message: this.message,
      status: this.status,
      requestId: this.requestId,
      details: this.details,
      retryable: this.status >= 500 || this.code === "NETWORK_ERROR",
    };
  }
}

/** 未登录 / 令牌失效，由上层决定跳转登录页 */
export class UnauthenticatedError extends HttpError {
  constructor() {
    super({
      code: "AUTH_TOKEN_EXPIRED",
      message: "登录状态已失效，请重新登录。",
      status: 401,
    });
  }
}

/* ------------------------------------------------------------------ */
/*  令牌存取                                                           */
/* ------------------------------------------------------------------ */

export type TokenBundle = {
  accessToken: string;
  refreshToken: string;
};

/**
 * 令牌存放位置由前端自行决定（契约 2.2），API 不作限制。
 * 这里用 localStorage；契约测试反向断言服务端不下发 Cookie，因此不使用 Cookie。
 *
 * 键名导出给**跨标签页登录同步**使用：另一个标签页登录/退出会改写它，
 * 本页据此重新拉取身份（见 ``AuthSessionBridge``）。
 */
export const TOKEN_KEY = "studybuddy.tokens";

export const tokenStorage = {
  read(): TokenBundle | null {
    try {
      const raw = window.localStorage.getItem(TOKEN_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as Partial<TokenBundle>;
      if (!parsed.accessToken || !parsed.refreshToken) return null;
      return { accessToken: parsed.accessToken, refreshToken: parsed.refreshToken };
    } catch {
      return null;
    }
  },
  write(tokens: TokenBundle): void {
    window.localStorage.setItem(TOKEN_KEY, JSON.stringify(tokens));
  },
  /** 刷新成功只更新 Access Token，保留原 Refresh Token（契约 2.4） */
  updateAccessToken(accessToken: string): void {
    const current = this.read();
    if (!current) return;
    this.write({ accessToken, refreshToken: current.refreshToken });
  },
  clear(): void {
    window.localStorage.removeItem(TOKEN_KEY);
  },
};

/* ------------------------------------------------------------------ */
/*  刷新与未认证回调                                                    */
/* ------------------------------------------------------------------ */

/** 由 auth feature 注入，避免 http 层反向依赖业务模块 */
let refreshHandler: (() => Promise<string | null>) | null = null;
let onUnauthenticated: (() => void) | null = null;

export function configureHttp(options: {
  refresh?: () => Promise<string | null>;
  unauthenticated?: () => void;
}): void {
  if (options.refresh) refreshHandler = options.refresh;
  if (options.unauthenticated) onUnauthenticated = options.unauthenticated;
}

function baseUrl(): string {
  const value = import.meta.env.VITE_API_BASE_URL;
  return (value ?? "").replace(/\/+$/, "");
}

/** 拼查询串；undefined / null 的键会被跳过 */
export function buildQuery(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) continue;
    search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}

/* ------------------------------------------------------------------ */
/*  请求                                                               */
/* ------------------------------------------------------------------ */

export type RequestOptions = {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  /** 是否需要携带 Bearer；登录、注册、刷新传 false */
  auth?: boolean;
  /** 内部使用：401 刷新后的重放标记，防止无限递归 */
  retried?: boolean;
};

async function parseError(response: Response): Promise<HttpError> {
  let code: ApiErrorCode = "INTERNAL_ERROR";
  let message = "服务暂时不可用，请稍后重试。";
  let requestId: string | undefined;
  let details: Record<string, unknown> = {};

  try {
    const payload = (await response.json()) as {
      error?: {
        code?: ApiErrorCode;
        message?: string;
        details?: Record<string, unknown> | null;
        request_id?: string;
      };
    };
    if (payload.error) {
      code = payload.error.code ?? code;
      message = payload.error.message ?? message;
      requestId = payload.error.request_id;
      details = payload.error.details ?? {};
    }
  } catch {
    // 响应体不是 JSON：保留兜底文案，不把原始异常暴露给用户
  }

  return new HttpError({ code, message, status: response.status, requestId, details });
}

/**
 * 401 处理策略：只重放一次，且刷新走 single-flight。
 * 契约 2.4 规定 Refresh Token 不轮换，重复刷新会放大请求量，
 * 因此多个并发 401 必须共用同一次刷新。
 */
let refreshInFlight: Promise<string | null> | null = null;

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, signal, auth = true, retried = false } = options;

  const headers = new Headers();
  if (body !== undefined) headers.set("Content-Type", "application/json; charset=utf-8");

  const tokens = tokenStorage.read();
  if (auth && tokens) headers.set("Authorization", `Bearer ${tokens.accessToken}`);

  let response: Response;
  try {
    response = await fetch(`${baseUrl()}${path}`, {
      method,
      headers,
      signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new HttpError({
      code: "NETWORK_ERROR",
      message: "网络连接失败，请检查网络后重试。",
      status: 0,
    });
  }

  if (response.status === 401 && auth) {
    if (!retried && refreshHandler) {
      refreshInFlight ??= refreshHandler().finally(() => {
        refreshInFlight = null;
      });
      const next = await refreshInFlight;
      if (next) {
        tokenStorage.updateAccessToken(next);
        return request<T>(path, { ...options, retried: true });
      }
    }
    tokenStorage.clear();
    onUnauthenticated?.();
    throw new UnauthenticatedError();
  }

  if (response.status === 204) return undefined as T;

  if (!response.ok) throw await parseError(response);

  const text = await response.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

export const http = {
  get: <T>(path: string, options?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...options, method: "GET" }),
  post: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...options, method: "POST", body }),
  patch: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...options, method: "PATCH", body }),
  delete: <T>(path: string, options?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...options, method: "DELETE" }),
};

/** 把任意异常收敛成 UI 可直接消费的 AppError */
export function toAppError(error: unknown): AppError {
  if (error instanceof HttpError) return error.toAppError();
  return {
    code: "INTERNAL_ERROR",
    message: "发生了未预期的错误，请稍后重试。",
    details: {},
    retryable: true,
  };
}
