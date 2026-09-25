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
  // 与"模型暂时超时"区分开：这类失败重试无用，需要改配置（开发方案 7.2）
  MODEL_TOOL_CALL_UNSUPPORTED: "模型服务不支持工具调用",
  TOOL_CALL: "工具调用失败",
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
    case "MODEL_TOOL_CALL_UNSUPPORTED":
      return "当前配置的模型服务不支持工具调用，需要在服务端换成支持 function calling 的模型。";
    case "TOOL_CALL":
      return "这次没能完成站内操作，可以换个说法重新提问。";
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

/**
 * 工具错误码 → 用户可见文案（开发方案第 8 节）。
 *
 * 工具失败时不能只显示"未知错误"：权限、资源不可见、参数不全这些情况
 * 用户能看懂，也知道下一步做什么。
 */
export const TOOL_ERROR_LABEL: Record<string, string> = {
  UNKNOWN_TOOL: "这次请求的能力当前不可用",
  INVALID_TOOL_ARGUMENTS: "调用参数不完整或不合法",
  FORBIDDEN: "当前账号没有执行这个操作的权限",
  RESOURCE_NOT_FOUND: "目标资源不存在或不可见",
  INVALID_STATE: "当前状态不允许执行这个操作",
  WRITE_LIMIT_REACHED: "本次对话已经创建过一次，未重复创建",
  WRITE_INTENT_REQUIRED: "还需要明确说明要生成什么、生成多少",
  SKILL_NOT_FOUND: "没有找到对应的任务工作流",
  SKILL_BUDGET_EXCEEDED: "本次可加载的工作流说明已达上限",
  AGENT_LOOP_LIMIT: "本次执行步骤过多，已停止",
  INTERNAL: "站内操作执行失败",
};

export function toolErrorLabel(code: string | null | undefined): string | null {
  if (!code) return null;
  return TOOL_ERROR_LABEL[code] ?? "站内操作失败";
}
