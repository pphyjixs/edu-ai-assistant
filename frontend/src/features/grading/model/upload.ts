/**
 * 学生报告上传的三段式编排（契约 9.2 / 9.3）。
 *
 *   本地校验 → 计算 sha256 → 初始化（后端签发）→ 直传对象存储 → 完成确认
 *
 * 与课件上传（第 4 节）流程一致，但有三处**必须**不同：
 *
 * 1. **只接受 PDF 与 DOCX**：`.doc` 与 `.pptx` 在后端返回
 *    `422 UPLOAD_INVALID`，前端提前用白名单拦住，避免白传一遍；
 * 2. 初始化路径挂在**作业**下（`/assignments/{id}/submissions/uploads`），
 *    因为提交身份由作业与当前学生共同确定，客户端不传 `student_id`；
 * 3. 同一份作业重复上传会复用仍处于 `UPLOADING` 的提交（同一 `submission_id`）
 *    并签发新会话——旧预签名地址随即失效，因此**不能**缓存上一次的上传地址重试，
 *    每次都要重新初始化。
 *
 * 每一步都通过 ``onStep`` 回报，界面据此给出可感知的进度。
 */

import {
  checkLocalFile,
  describeLocalProblem,
  putFileToStorage,
  sha256Hex,
  SUBMISSION_EXTENSIONS,
} from "@/services/upload";

import { gradingApi, type SubmissionDetailDto } from "../api";

export type UploadStep = "checking" | "hashing" | "initializing" | "uploading" | "completing";

export const UPLOAD_STEP_LABEL: Record<UploadStep, string> = {
  checking: "校验文件",
  hashing: "计算校验和",
  initializing: "申请上传地址",
  uploading: "上传报告",
  completing: "确认提交",
};

export class SubmissionUploadProblem extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SubmissionUploadProblem";
  }
}

export type UploadSubmissionParams = {
  assignmentId: string;
  file: File;
  onStep?: (step: UploadStep) => void;
  signal?: AbortSignal;
};

export async function uploadSubmission({
  assignmentId,
  file,
  onStep,
  signal,
}: UploadSubmissionParams): Promise<SubmissionDetailDto> {
  onStep?.("checking");

  // 本地先挡一次明显不合规的文件；后端仍会独立校验并返回 UPLOAD_INVALID
  const local = checkLocalFile(file, undefined, SUBMISSION_EXTENSIONS);
  if (local.kind !== "ok") {
    throw new SubmissionUploadProblem(describeLocalProblem(local));
  }

  onStep?.("hashing");
  const sha256 = await sha256Hex(file);

  onStep?.("initializing");
  const init = await gradingApi.initUpload(assignmentId, {
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
  return gradingApi.completeUpload(assignmentId, init.upload_id);
}
