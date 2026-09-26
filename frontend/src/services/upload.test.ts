/**
 * 上传上限与本地校验（契约 4.2）。
 *
 * 单文件上限是**三处共同约定**的值：前端 `DEFAULT_MAX_UPLOAD_BYTES`、
 * 后端 `MATERIAL_MAX_UPLOAD_BYTES` / `ASSIGNMENT_ATTACHMENT_MAX_UPLOAD_BYTES` /
 * `SUBMISSION_MAX_UPLOAD_BYTES`、以及 nginx 的 `client_max_body_size`。
 * 这里把前端这一处钉住，避免只改一边造成「前端放行、后端拒绝」的割裂。
 */

import { describe, expect, it } from "vitest";

import {
  DEFAULT_MAX_UPLOAD_BYTES,
  RUBRIC_SUGGEST_MAX_BYTES,
  RUBRIC_SUGGEST_MAX_MB,
  checkLocalFile,
  describeLocalProblem,
} from "@/services/upload";

function pdfFile(name: string, bytes: number): File {
  return new File([new Uint8Array(bytes)], name, { type: "application/pdf" });
}

describe("上传大小上限", () => {
  it("统一为 5 MiB", () => {
    expect(DEFAULT_MAX_UPLOAD_BYTES).toBe(5 * 1024 * 1024);
  });

  it("恰好等于上限的文件可以通过", () => {
    expect(checkLocalFile(pdfFile("a.pdf", DEFAULT_MAX_UPLOAD_BYTES))).toEqual({
      kind: "ok",
      contentType: "application/pdf",
    });
  });

  it("超出上限一个字节就被拦下，并回显当前上限", () => {
    const problem = checkLocalFile(pdfFile("a.pdf", DEFAULT_MAX_UPLOAD_BYTES + 1));
    expect(problem).toEqual({ kind: "too-large", maxBytes: DEFAULT_MAX_UPLOAD_BYTES });
    // 文案由上限换算得出，改常量后提示会跟着变成 5 MB
    expect(describeLocalProblem(problem)).toBe("文件超过当前上限（5 MB）。");
  });

  it("空文件仍按空文件拒绝，而不是当成超限", () => {
    expect(checkLocalFile(pdfFile("a.pdf", 0))).toEqual({ kind: "empty-file" });
  });

  it("自动解析评分项的上限与单文件上限同值（5 MiB）", () => {
    expect(RUBRIC_SUGGEST_MAX_BYTES).toBe(DEFAULT_MAX_UPLOAD_BYTES);
    expect(RUBRIC_SUGGEST_MAX_MB).toBe(5);

    const over = pdfFile("a.pdf", RUBRIC_SUGGEST_MAX_BYTES + 1);
    expect(checkLocalFile(over, RUBRIC_SUGGEST_MAX_BYTES)).toEqual({
      kind: "too-large",
      maxBytes: RUBRIC_SUGGEST_MAX_BYTES,
    });
  });

  it("调用方仍可显式传入更小的上限", () => {
    const file = pdfFile("a.pdf", 1024 * 1024 + 1);
    expect(checkLocalFile(file, 1024 * 1024)).toEqual({
      kind: "too-large",
      maxBytes: 1024 * 1024,
    });
  });
});
