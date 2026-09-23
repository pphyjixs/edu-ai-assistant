/**
 * 失败阶段码 → 用户可见说明。
 *
 * 后端把「失败发生在哪一步」落成阶段码（`jobs.failure_stage` /
 * `materials.failure_stage`，见 `app.modules.jobs.models.JobFailureStage`），
 * 因此前端可以区分「文件读不出来」「文本提取不出来」「模型侧失败」，
 * 而不是所有失败都显示同一句话（评审文档「一、#4.8 / #9」）。
 *
 * 放在 utils 里是因为资料解析与 Buddy 问答两条链路共用同一套阶段码。
 */

export const FAILURE_STAGE_LABEL: Record<string, string> = {
  DOWNLOAD: "读取文件失败",
  NATIVE_EXTRACT: "文本提取失败",
  RENDER: "页面渲染失败",
  VISION_OCR: "图片识别失败",
  OUTLINE_GENERATION: "内容整理失败",
  MODEL_CALL: "模型服务调用失败",
  PUBLISH: "结果写入失败",
  UNKNOWN: "执行失败",
};

export function failureStageLabel(stage: string | null | undefined): string {
  if (!stage) return "执行失败";
  return FAILURE_STAGE_LABEL[stage] ?? "执行失败";
}

/** 失败时给用户的可执行提示；不同阶段能做的事不一样 */
export function failureStageHint(stage: string | null | undefined): string {
  switch (stage) {
    case "MODEL_CALL":
      return "模型服务这次没有响应成功，稍后重新提问通常就能恢复。";
    case "DOWNLOAD":
      return "服务端没能读到这个文件，可以重新上传后再试。";
    case "NATIVE_EXTRACT":
      return "文件里没有提取到可用的文本（例如扫描版 PDF），换一份可复制文字的课件试试。";
    case "PUBLISH":
      return "结果没能写入，请重新发起一次。";
    default:
      return "可以重新发起一次；如果一直失败，稍后再试。";
  }
}
