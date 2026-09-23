/**
 * 浏览器直传对象存储。
 *
 * 契约依据：docs/api-contract.md 第 4.1–4.4 节。
 *
 * 流程：初始化（后端签发）→ 用返回的 method + headers 原样 PUT 文件字节
 * → 调用完成接口。本模块只负责「本地校验」与「真正把字节发出去」两件事，
 * 三段式的编排在 features/materials/api 里完成。
 */

/**
 * 契约 4.2：扩展名 → 规范 MIME，必须精确相等。
 * 不接受 application/octet-stream、近似类型或带参数的形式。
 */
export const CANONICAL_MIME_BY_EXTENSION: Record<string, string> = {
  pdf: "application/pdf",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
};

/** 契约 4.2：默认上限 50 MiB；真实上限由后端 details.max_size_bytes 回显 */
export const DEFAULT_MAX_UPLOAD_BYTES = 52_428_800;

export const ACCEPTED_EXTENSIONS = Object.keys(CANONICAL_MIME_BY_EXTENSION);

export function fileExtension(filename: string): string {
  const index = filename.lastIndexOf(".");
  return index === -1 ? "" : filename.slice(index + 1).toLowerCase();
}

/**
 * 契约 9.2：**提交报告只接受 PDF 与 DOCX**（不像课件上传还允许 PPTX）。
 *
 * 因此允许的扩展名做成参数而不是写死一份。
 */
export const SUBMISSION_EXTENSIONS = ["pdf", "docx"] as const;

export type LocalFileProblem =
  | { kind: "ok"; contentType: string }
  | { kind: "empty-name" }
  | { kind: "too-long" }
  | { kind: "bad-chars" }
  | { kind: "unsupported-type"; allowed: string[] }
  | { kind: "empty-file" }
  | { kind: "too-large"; maxBytes: number };

/**
 * 上传前的本地校验。
 *
 * 这不是「前端代替后端校验」——后端仍会独立校验并返回 UPLOAD_INVALID；
 * 这里提前拦住只是为了避免把 50 MB 的文件白传一遍。
 *
 * @param allowedExtensions 允许的扩展名（小写、不含点）；省略时用课件上传的清单
 */
export function checkLocalFile(
  file: File,
  maxBytes = DEFAULT_MAX_UPLOAD_BYTES,
  allowedExtensions: readonly string[] = ACCEPTED_EXTENSIONS,
): LocalFileProblem {
  const name = file.name.trim();

  if (name.length === 0) return { kind: "empty-name" };
  if (name.length > 255) return { kind: "too-long" };
  if (/[/\\\u0000]/.test(name)) return { kind: "bad-chars" };

  const extension = fileExtension(name);
  const contentType = CANONICAL_MIME_BY_EXTENSION[extension];
  if (!contentType || !allowedExtensions.includes(extension)) {
    return { kind: "unsupported-type", allowed: [...allowedExtensions] };
  }

  if (file.size < 1) return { kind: "empty-file" };
  if (file.size > maxBytes) return { kind: "too-large", maxBytes };

  return { kind: "ok", contentType };
}

export function describeLocalProblem(problem: LocalFileProblem): string {
  switch (problem.kind) {
    case "empty-name":
      return "请选择文件后再上传。";
    case "too-long":
      return "文件名过长，请重命名为 255 个字符以内。";
    case "bad-chars":
      return "文件名不能包含 / 、\\ 等路径分隔符。";
    case "unsupported-type":
      // 文案按调用方给的白名单生成：提交页不能出现"只支持 PPTX"这种错误提示
      return `只支持 ${problem.allowed.map((item) => item.toUpperCase()).join("、")} 格式。`;
    case "empty-file":
      return "文件内容为空，无法上传。";
    case "too-large":
      return `文件超过当前上限（${Math.floor(problem.maxBytes / 1024 / 1024)} MB）。`;
    case "ok":
      return "";
  }
}

/**
 * 计算文件 sha256（小写十六进制）。
 *
 * 契约 4.2 要求请求里带上 64 位十六进制摘要，由浏览器计算；
 * 后端不会在应用侧重读文件比对，而是把摘要放进签名头交给对象存储校验。
 */
export async function sha256Hex(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

export type StorageUploadTarget = {
  uploadUrl: string;
  method: string;
  /** 契约 4.4：三个签名头必须完整、原样发送，增删改任一都会导致签名不符 */
  headers: Record<string, string>;
};

export class StorageUploadError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "StorageUploadError";
    this.status = status;
  }
}

/**
 * 把文件字节流 PUT 到预签名地址。
 * 请求体是文件原始字节，不使用 multipart 或额外包装（契约 4.4）。
 */
export async function putFileToStorage(
  target: StorageUploadTarget,
  file: File,
  signal?: AbortSignal,
): Promise<void> {
  const headers = new Headers();
  for (const [key, value] of Object.entries(target.headers)) {
    headers.set(key, value);
  }

  let response: Response;
  try {
    response = await fetch(target.uploadUrl, {
      method: target.method,
      headers,
      body: file,
      signal,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new StorageUploadError(
      "上传到对象存储失败，请检查网络后重试。",
      0,
    );
  }

  if (response.ok) return;

  // 412：If-None-Match 条件写入发现对象已存在（重复上传同一 upload_id）
  if (response.status === 412) {
    throw new StorageUploadError("该对象已存在，请重新选择文件后再上传。", 412);
  }

  // 403：预签名地址已过期（默认 10 分钟）
  if (response.status === 403 || response.status === 401) {
    throw new StorageUploadError("上传地址已过期，请重新上传。", response.status);
  }

  throw new StorageUploadError(
    `上传失败（HTTP ${response.status}），请重试。`,
    response.status,
  );
}
