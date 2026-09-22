/**
 * 三段式上传的编排（契约 4.1）。
 *
 *   本地校验 → 计算 sha256 → 初始化（后端签发）→ 直传对象存储 → 完成确认
 *
 * 每一步都会通过 ``onStep`` 回报，便于界面给出可感知的进度，
 * 而不是让用户面对一个没有反馈的等待。
 */

import { checkLocalFile, describeLocalProblem, putFileToStorage, sha256Hex } from "@/services/upload";

import { materialsApi, type MaterialUploadCompleteResponseDto } from ".";

export type UploadStep = "checking" | "hashing" | "initializing" | "uploading" | "completing";

export const UPLOAD_STEP_LABEL: Record<UploadStep, string> = {
  checking: "校验文件",
  hashing: "计算校验和",
  initializing: "申请上传地址",
  uploading: "上传到对象存储",
  completing: "确认上传",
};

export class MaterialUploadProblem extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MaterialUploadProblem";
  }
}

export type UploadMaterialParams = {
  courseId: string;
  file: File;
  onStep?: (step: UploadStep) => void;
  signal?: AbortSignal;
};

export async function uploadMaterial({
  courseId,
  file,
  onStep,
  signal,
}: UploadMaterialParams): Promise<MaterialUploadCompleteResponseDto> {
  onStep?.("checking");

  // 本地先挡一次明显不合规的文件，避免把 50 MB 白传一遍；
  // 后端仍会独立校验并返回 UPLOAD_INVALID。
  const local = checkLocalFile(file);
  if (local.kind !== "ok") {
    throw new MaterialUploadProblem(describeLocalProblem(local));
  }

  onStep?.("hashing");
  const sha256 = await sha256Hex(file);

  onStep?.("initializing");
  const init = await materialsApi.initUpload(courseId, {
    filename: file.name.trim(),
    content_type: local.contentType,
    size: file.size,
    sha256,
  });

  onStep?.("uploading");
  // 契约 4.4：method 与 headers 必须原样使用，请求体是文件原始字节
  await putFileToStorage(
    { uploadUrl: init.upload_url, method: init.method, headers: init.headers },
    file,
    signal,
  );

  onStep?.("completing");
  return materialsApi.completeUpload(courseId, init.upload_id);
}
