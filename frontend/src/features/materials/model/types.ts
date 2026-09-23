/**
 * 资料与大纲的视图模型。
 *
 * 契约 4.7 明确：完成确认后资料即为 `PROCESSING`，由解析 Worker 推进；
 * 界面上 `PROCESSING` 一律展示为「解析中」，只有 `FAILED` 才是失败——
 * 不能把排队中的资料画成错误状态。
 */

import type { PillTone } from "@/components/Pill/Pill";
import { formatMonthDayTime } from "@/utils/datetime";
import { failureStageHint, failureStageLabel } from "@/utils/failureStage";
import { formatBytes } from "@/utils/format";
import { formatLocation, SOURCE_TYPE_LABEL } from "@/utils/location";

import type {
  MaterialDetailDto,
  MaterialKnowledgePointDto,
  MaterialOutlineDto,
  MaterialSectionDto,
  MaterialStatusDto,
} from "../api";

export type MaterialStatus = "uploading" | "uploaded" | "processing" | "ready" | "failed";

const STATUS_MAP: Record<
  MaterialStatusDto,
  { status: MaterialStatus; label: string; tone: PillTone }
> = {
  UPLOADING: { status: "uploading", label: "上传中", tone: "neutral" },
  UPLOADED: { status: "uploaded", label: "已上传", tone: "neutral" },
  PROCESSING: { status: "processing", label: "解析中", tone: "info" },
  READY: { status: "ready", label: "解析完成", tone: "success" },
  FAILED: { status: "failed", label: "解析失败", tone: "danger" },
};

/** 规范 MIME → 界面上的格式标签；不认识的类型回退到扩展名 */
const TYPE_LABEL: Record<string, string> = {
  "application/pdf": "PDF",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PPTX",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
};

function typeLabelOf(filename: string, contentType: string): string {
  const known = TYPE_LABEL[contentType];
  if (known) return known;
  const index = filename.lastIndexOf(".");
  return index === -1 ? "文件" : filename.slice(index + 1).toUpperCase();
}

export type MaterialVM = {
  id: string;
  courseId: string;
  filename: string;
  typeLabel: string;
  sizeLabel: string;
  status: MaterialStatus;
  statusLabel: string;
  statusTone: PillTone;
  /** 仅 FAILED 时非空；后端返回的安全描述 */
  errorMessage: string | null;
  /**
   * 仅 FAILED 时非空的失败阶段码（``DOWNLOAD`` / ``NATIVE_EXTRACT`` /
   * ``OUTLINE_GENERATION`` …）。前端据此说明「失败在哪一步、能做什么」，
   * 而不是只给一句笼统的失败原因（评审文档「一、#4.8」）。
   */
  failureStage: string | null;
  /** 失败阶段的人话说明；非失败时为 null */
  failureStageLabel: string | null;
  /** 失败时给用户的可执行建议；非失败时为 null */
  failureHint: string | null;
  createdAtLabel: string;
  /** 上传教师显示名；教师已注销时为空（此时界面不显示上传者） */
  uploadedByName: string | null;
  /** 客户端上传时声明的 SHA-256；历史完成快照可能为空 */
  sha256: string | null;
  /** 只有解析完成才能读大纲 */
  isReady: boolean;
  /** 契约 5.3：重试解析对已完成的资料会被拒绝，因此只在失败时提供入口 */
  canRetry: boolean;
};

export function toMaterialVM(dto: MaterialDetailDto): MaterialVM {
  const mapped = STATUS_MAP[dto.status];
  const failed = mapped.status === "failed";

  return {
    id: dto.id,
    courseId: dto.course_id,
    filename: dto.filename,
    typeLabel: typeLabelOf(dto.filename, dto.content_type),
    sizeLabel: formatBytes(dto.size),
    status: mapped.status,
    statusLabel: mapped.label,
    statusTone: mapped.tone,
    errorMessage: dto.error_message,
    failureStage: failed ? (dto.failure_stage ?? null) : null,
    failureStageLabel: failed ? failureStageLabel(dto.failure_stage) : null,
    failureHint: failed ? failureStageHint(dto.failure_stage) : null,
    createdAtLabel: formatMonthDayTime(dto.created_at),
    uploadedByName: dto.uploaded_by_name ?? null,
    sha256: dto.sha256 ?? null,
    isReady: mapped.status === "ready",
    canRetry: failed,
  };
}

/* ------------------------------- 大纲 ------------------------------- */

export type KnowledgePointVM = {
  id: string;
  order: number;
  title: string;
  description: string;
  /** 可核对的原文摘录 */
  quote: string;
  locationLabel: string;
};

export type MaterialSectionVM = {
  id: string;
  order: number;
  title: string;
  locationLabel: string;
  sourceTypeLabel: string;
  knowledgePoints: KnowledgePointVM[];
};

export type MaterialOutlineVM = {
  materialId: string;
  sections: MaterialSectionVM[];
  knowledgePointCount: number;
};

function toKnowledgePointVM(dto: MaterialKnowledgePointDto): KnowledgePointVM {
  return {
    id: dto.id,
    order: dto.order,
    title: dto.title,
    description: dto.description,
    quote: dto.quote,
    locationLabel: formatLocation(null, dto.location_start, dto.location_end),
  };
}

function toSectionVM(dto: MaterialSectionDto): MaterialSectionVM {
  return {
    id: dto.id,
    order: dto.order,
    title: dto.title,
    sourceTypeLabel: SOURCE_TYPE_LABEL[dto.source_type] ?? "来源",
    locationLabel: formatLocation(dto.source_type, dto.location_start, dto.location_end),
    knowledgePoints: dto.knowledge_points.map(toKnowledgePointVM),
  };
}

export function toMaterialOutlineVM(dto: MaterialOutlineDto): MaterialOutlineVM {
  const sections = dto.sections.map(toSectionVM);
  return {
    materialId: dto.material_id,
    sections,
    knowledgePointCount: sections.reduce(
      (sum, section) => sum + section.knowledgePoints.length,
      0,
    ),
  };
}
