/**
 * 统一的 HTTP 边界。
 *
 * 现状：除 auth 外后端业务接口尚未实现，业务页面一律通过 feature 级
 * mock adapter 取数（见 docs/api-contract.md 与 API_INTEGRATION_NOTES.md）。
 * 该客户端先建立好协议边界，等后端补齐后只需在 feature 的 api/index.ts
 * 里把 mock 换成基于这里的实现，页面与组件不需要改动。
 *
 * 契约依据（docs/api-contract.md 第 1、2 节）：
 * - 基础路径由 VITE_API_BASE_URL 提供，形如 ``http://localhost:8000/api/v1``
 * - 认证头 ``Authorization: Bearer <access_token>``
 * - 错误统一为 ``{ error: { code, message, details, request_id } }``
 * - 401 使用 AUTH_TOKEN_EXPIRED；Access Token 有效期 1 小时
 */

export type ApiErrorCode =
  | "AUTH_INVALID_CREDENTIALS"
  | "AUTH_TOKEN_EXPIRED"
  | "AUTH_EMAIL_TAKEN"
  | "AUTH_TOO_MANY_ATTEMPTS"
  | "ROLE_FORBIDDEN"
  | "COURSE_FORBIDDEN"
  | "RESOURCE_NOT_FOUND"
  | "INVITE_CODE_INVALID"
  | "UPLOAD_INVALID"
  | "RUBRIC_SCORE_MISMATCH"
  | "MATERIAL_NOT_READY"
  | "ASSIGNMENT_NOT_OPEN"
  | "GRADE_NOT_REVIEWED"
  | "AI_JOB_FAILED"
  | "VALIDATION_ERROR"
  | "METHOD_NOT_ALLOWED"
  | "INTERNAL_ERROR"
  | "SERVICE_UNAVAILABLE"
  | "NETWORK_ERROR";

export type AppError = {
  code: ApiErrorCode;
  /** 面向用户的可读文案；后端 message 缺失时由本层兜底 */
  message: string;
  status?: number;
  requestId?: string;
  /** 由调用方决定是否展示重试入口（见 API_INTEGRATION_NOTES 第 7 节） */
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
      retryable: this.status >= 500 || this.code === "NETWORK_ERROR",
    };
  }
}

export type TokenBundle = {
  accessToken: string;
  refreshToken: string;
};

/**
 * 令牌读写。存放位置由前端自行决定（契约 2.2），
 * 这里先用 localStorage；后续接入真实登录时替换实现即可。
 */
const TOKEN_KEY = "studybuddy.tokens";

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
  clear(): void {
    window.localStorage.removeItem(TOKEN_KEY);
  },
};

/** 未登录时抛出的错误，由上层决定跳转登录页 */
export class UnauthenticatedError extends HttpError {
  constructor() {
    super({
      code: "AUTH_TOKEN_EXPIRED",
      message: "登录状态已失效，请重新登录。",
      status: 401,
    });
  }
}

/** 刷新回调由 auth feature 注入，避免 http 层反向依赖业务模块 */
let refreshHandler: (() => Promise<string | null>) | null = null;
let onUnauthenticated: (() => void) | null = null;

export function configureHttp(options: {
  refresh?: () => Promise<string | null>;
  unauthenticated?: () => void;
}): void {
  refreshHandler = options.refresh ?? null;
  onUnauthenticated = options.unauthenticated ?? null;
}

function baseUrl(): string {
  const value = import.meta.env.VITE_API_BASE_URL;
  return (value ?? "").replace(/\/+$/, "");
}

type RequestOptions = {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  /** 是否需要携带 Bearer；如 /auth/refresh、/auth/login 传 false */
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
        details?: Record<string, unknown>;
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
 * 唯一的请求入口。页面与组件不得直接使用 fetch（见 docs/architecture.md 第 5 节）。
 *
 * 401 处理策略：只重放一次，且刷新走 single-flight，避免多个并发请求
 * 同时触发刷新（契约 2.4 规定 Refresh Token 不轮换，重复刷新会放大请求量）。
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
        tokenStorage.write({ accessToken: next, refreshToken: tokens?.refreshToken ?? "" });
        return request<T>(path, { ...options, retried: true });
      }
    }
    tokenStorage.clear();
    onUnauthenticated?.();
    throw new UnauthenticatedError();
  }

  if (response.status === 204) return undefined as T;

  if (!response.ok) throw await parseError(response);

  return (await response.json()) as T;
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
    retryable: true,
  };
}
