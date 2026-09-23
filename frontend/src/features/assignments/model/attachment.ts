/**
 * 作业附件的视图模型与上传编排（契约 8.15）。
 *
 * 附件走的是**与课件完全相同的三段式上传协议**（4.1–4.5），因此这里直接复用
 * `services/upload` 的校验、摘要计算与直传实现，只把路径换成挂在任务下的那一组。
 *
 * 允许的类型也与课件一致（PDF / PPTX / DOCX）；差异在权限与状态：
 * 附件只有课程创建教师能写，且**草稿、进行中、已关闭**的任务都能改
 * （附件是教师自己的参考资料，与学生能否提交无关）。
 */

import {
  checkLocalFile,
  describeLocalProblem,
  putFileToStorage,
  sha256Hex,
} from "@/services/upload";
import type { PillTone } from "@/components/Pill/Pill";
import { formatMonthDayTime } from "@/utils/datetime";
import { formatBytes } from "@/utils/format";

import { assignmentsApi, type AssignmentAttachmentDto } from "../api";

export type AttachmentVM = {
  id: string;
  filename: string;
  /** PDF / PPTX / DOCX */
  typeLabel: string;
  sizeLabel: string;
  /** 展示用的短摘要；完整值放在 title 里 */
  sha256Short: string;
  sha256: string;
  uploadedByName: string | null;
  /** 预签名下载地址（短时有效） */
  downloadUrl: string;
  downloadExpiresLabel: string;
  createdAtLabel: string;
};

const TYPE_LABEL: Record<string, string> = {
  "application/pdf": "PDF",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PPTX",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
};

export function toAttachmentVM(dto: AssignmentAttachmentDto): AttachmentVM {
  const extension = dto.filename.split(".").pop() ?? "";
  return {
    id: dto.id,
    filename: dto.filename,
    typeLabel: TYPE_LABEL[dto.content_type] ?? (extension.toUpperCase() || "文件"),
    sizeLabel: formatBytes(dto.size),
    sha256Short: dto.sha256 ? `${dto.sha256.slice(0, 12)}…` : "",
    sha256: dto.sha256,
    uploadedByName: dto.uploaded_by_name ?? null,
    downloadUrl: dto.download_url,
    downloadExpiresLabel: formatMonthDayTime(dto.download_expires_at),
    createdAtLabel: formatMonthDayTime(dto.created_at),
  };
}

/** 附件没有"进行中/失败"这类业务状态，因此只需要上传过程与错误 */
export type AttachmentTone = PillTone;

/* ------------------------------ 上传编排 ------------------------------ */

export type UploadStep = "checking" | "hashing" | "initializing" | "uploading" | "completing";

export const UPLOAD_STEP_LABEL: Record<UploadStep, string> = {
  checking: "校验文件",
  hashing: "计算校验和",
  initializing: "申请上传地址",
  uploading: "上传附件",
  completing: "确认附件",
};

export class AttachmentUploadProblem extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AttachmentUploadProblem";
  }
}

export type UploadAttachmentParams = {
  assignmentId: string;
  file: File;
  onStep?: (step: UploadStep) => void;
  signal?: AbortSignal;
};

export async function uploadAttachment({
  assignmentId,
  file,
  onStep,
  signal,
}: UploadAttachmentParams): Promise<AssignmentAttachmentDto> {
  onStep?.("checking");

  // 本地先挡一次明显不合规的文件；后端仍会独立校验并返回 UPLOAD_INVALID
  const local = checkLocalFile(file);
  if (local.kind !== "ok") {
    throw new AttachmentUploadProblem(describeLocalProblem(local));
  }

  onStep?.("hashing");
  const sha256 = await sha256Hex(file);

  onStep?.("initializing");
  const init = await assignmentsApi.initAttachmentUpload(assignmentId, {
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
  return assignmentsApi.completeAttachmentUpload(assignmentId, init.upload_id);
}
