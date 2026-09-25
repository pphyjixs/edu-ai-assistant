/**
 * Buddy 会话与 Agent Run 接口。
 *
 * 后端 `chat` 与 `agent` 两个模块都已实现：
 *
 * - 会话与历史：`POST /courses/{id}/chat-sessions`、`GET .../messages`；
 * - 异步 Run：`POST /chat-sessions/{id}/runs`（202，立即返回）、
 *   `GET /agent-runs/{id}`（轮询）、`POST /agent-runs/{id}/cancel`。
 *
 * Buddy 的提问现在走 **Run**：页面用 `useSetBuddyContext` 声明的当前对象
 * （课程 / 资料 / 章节 / 作业）会随请求发给后端，由后端按权限加载上下文并在
 * 独立 Worker 里调用模型——API 不会等待数分钟的模型响应。
 *
 * 同步接口 `POST /chat-sessions/{id}/messages` 仍然保留（历史兼容），
 * 前端不再使用。
 */

import { buildQuery, http } from "@/services/http";
import type { Page, Schemas } from "@/types/api";

export type ChatSessionDto = Schemas["ChatSessionSchema"];
export type ChatMessageDto = Schemas["ChatMessageSchema"];
export type CitationDto = Schemas["Citation"];
export type AgentRunDto = Schemas["AgentRunSchema"];
export type AgentRunSourceDto = Schemas["AgentRunSourceSchema"];
export type AgentRunCreateRequestDto = Schemas["AgentRunCreateRequest"];
export type AgentRunActionDto = Schemas["AgentRunAction"];
export type AgentEntityTypeDto = Schemas["AgentEntityType"];
export type AgentRunStatusDto = AgentRunDto["status"];

/** 契约里 role 是普通字符串（"USER" / "ASSISTANT"），这里做一次收窄 */
export function isAssistantMessage(message: ChatMessageDto): boolean {
  return message.role === "ASSISTANT";
}

export function citationsOf(message: ChatMessageDto): CitationDto[] {
  return message.citations ?? [];
}

/**
 * 引用是否指向课程资料。
 *
 * 作业与评分标准也能支撑结论（评审文档「一、#2」），但它们没有页码，
 * 也不能跳到资料阅读器，因此前端要分流渲染。
 */
export function isMaterialCitation(citation: CitationDto): boolean {
  return citation.source_kind !== "ASSIGNMENT" && Boolean(citation.material_id);
}

/** 可直接展示的来源名称；资料引用退回到资料名 */
export function citationLabel(citation: CitationDto): string {
  return citation.source_label ?? citation.material_name ?? "来源";
}

/** Run 是否已结束（终态） */
export function isTerminalRunStatus(status: AgentRunStatusDto | undefined): boolean {
  return status === "SUCCEEDED" || status === "FAILED" || status === "CANCELLED";
}

/** Run 是否还在排队或执行中——UI 应显示进度而不是失败 */
export function isRunInFlight(status: AgentRunStatusDto | undefined): boolean {
  return status === "PENDING" || status === "RUNNING";
}

/**
 * 消息分页大小。
 *
 * 消息按 created_at **升序**返回（契约 6.1），因此最新消息在最后一页。
 * 先用一页的容量拿全量，只有真的超过一页时再补一次「最后一页」的请求。
 */
const MESSAGE_PAGE_SIZE = 100;

/** 会话内 Run 列表一次取回的条数（够覆盖当前可见的几轮问答即可） */
const RUN_LIST_LIMIT = 20;

export const buddyApi = {
  /** 契约 6.2：POST /courses/{course_id}/chat-sessions —— 没有请求字段 */
  createSession(courseId: string): Promise<ChatSessionDto> {
    return http.post<ChatSessionDto>(`/courses/${courseId}/chat-sessions`, {});
  },

  /**
   * 会话详情：``GET /chat-sessions/{session_id}``。
   *
   * 中央会话页刷新时只有 ``sessionId``，需要据此知道它属于哪门课程；
   * 只读接口，权限与消息列表一致（仅所有者可读，否则 404）。
   */
  getSession(sessionId: string, signal?: AbortSignal): Promise<ChatSessionDto> {
    return http.get<ChatSessionDto>(`/chat-sessions/${sessionId}`, { signal });
  },

  /** 契约 6.3：GET /courses/{course_id}/chat-sessions —— 按 last_message_at 倒序 */
  listSessions(courseId: string, pageSize = 20): Promise<Page<ChatSessionDto>> {
    return http.get<Page<ChatSessionDto>>(
      `/courses/${courseId}/chat-sessions${buildQuery({ page: 1, page_size: pageSize })}`,
    );
  },

  /** 契约 6.4：GET /chat-sessions/{session_id}/messages —— 按 created_at 升序 */
  async listMessages(sessionId: string, signal?: AbortSignal): Promise<ChatMessageDto[]> {
    const first = await http.get<Page<ChatMessageDto>>(
      `/chat-sessions/${sessionId}/messages${buildQuery({ page: 1, page_size: MESSAGE_PAGE_SIZE })}`,
      { signal },
    );

    if (first.total <= MESSAGE_PAGE_SIZE) return first.items;

    const lastPage = Math.ceil(first.total / MESSAGE_PAGE_SIZE);
    const last = await http.get<Page<ChatMessageDto>>(
      `/chat-sessions/${sessionId}/messages${buildQuery({
        page: lastPage,
        page_size: MESSAGE_PAGE_SIZE,
      })}`,
      { signal },
    );
    return last.items;
  },

  /**
   * 文档 6.2：POST /chat-sessions/{session_id}/runs
   * 202 立即返回；用户消息、Run 与 AGENT_RUN 任务在同一事务里写入。
   */
  createRun(sessionId: string, body: AgentRunCreateRequestDto): Promise<AgentRunDto> {
    return http.post<AgentRunDto>(`/chat-sessions/${sessionId}/runs`, body);
  },

  /** 文档 6.2：GET /agent-runs/{run_id} —— 状态、进度与错误来自关联任务 */
  getRun(runId: string, signal?: AbortSignal): Promise<AgentRunDto> {
    return http.get<AgentRunDto>(`/agent-runs/${runId}`, { signal });
  },

  /** 文档 6.2：POST /agent-runs/{run_id}/cancel */
  cancelRun(runId: string): Promise<AgentRunDto> {
    return http.post<AgentRunDto>(`/agent-runs/${runId}/cancel`, {});
  },

  /**
   * 评审文档「一、#11」：GET /chat-sessions/{session_id}/active-run
   *
   * 打开会话时先问一次「有没有还没结束的 Run」。有就按正常节奏恢复轮询，
   * 因此刷新页面不会让「正在生成」的提示消失，也不会丢掉已经发出的提问。
   * 返回的 `run` 为 null 是常规状态（没有进行中的任务），不是错误。
   */
  getActiveRun(
    sessionId: string,
    signal?: AbortSignal,
  ): Promise<Schemas["AgentActiveRunSchema"]> {
    return http.get<Schemas["AgentActiveRunSchema"]>(
      `/chat-sessions/${sessionId}/active-run`,
      { signal },
    );
  },

  /**
   * 评审文档「一、#11」：GET /chat-sessions/{session_id}/runs
   *
   * 会话内的 Run 列表（新→旧）。前端用它把 FAILED / CANCELLED 的原因
   * 显示在对应提问下面，这样刷新之后用户仍然知道上一次为什么没有回答。
   */
  listRuns(sessionId: string, limit = RUN_LIST_LIMIT, signal?: AbortSignal): Promise<Page<AgentRunDto>> {
    return http.get<Page<AgentRunDto>>(
      `/chat-sessions/${sessionId}/runs${buildQuery({ limit })}`,
      { signal },
    );
  },
};
