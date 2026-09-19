/**
 * Buddy 会话的 logical API 定义。
 *
 * 字段严格按 docs/api-contract.md 第 6 节（课程问答接口）书写，
 * 不新增、不改名、不发明 endpoint：
 *
 *   POST /courses/{course_id}/chat-sessions          创建会话
 *   GET  /courses/{course_id}/chat-sessions          我的会话列表
 *   GET  /chat-sessions/{session_id}/messages        会话消息
 *   POST /chat-sessions/{session_id}/messages        发送问题
 *
 * 回答响应同样按契约定义：id / role / content / grounded / citations / created_at。
 */

export type ChatRoleDto = "USER" | "ASSISTANT";

/** 契约 6 的引用结构，可点击跳转到资料定位 */
export type CitationDto = {
  material_id: string;
  material_name: string;
  section_id: string;
  section_title: string;
  page: number;
  quote: string;
};

export type ChatMessageDto = {
  id: string;
  role: ChatRoleDto;
  content: string;
  grounded: boolean;
  citations: CitationDto[];
  created_at: string;
};

export type ChatSessionDto = {
  id: string;
  course_id: string;
  title: string;
  created_at: string;
};

/** 契约 6 的提问请求体：只有 content 一个字段 */
export type SendMessageBody = {
  content: string;
};

/**
 * 发送消息的附加参数。
 *
 * 刻意与 ``SendMessageBody`` 分开：契约 6 的请求体只接受 ``{ content }``，
 * 没有承载上下文的字段，而产品要求 Buddy 自动知道当前课程 / 作业 / 资料。
 * 因此上下文走这个带外参数，由 adapter 决定怎么用——
 * 线上实现可以直接丢弃它（退化为只有 content），
 * 等契约新增可选字段后只需要改 adapter 一个函数。
 */
export type SendMessageOptions = {
  context?: unknown;
};

export type CreateSessionBody = {
  title?: string;
};

export interface BuddyApi {
  listSessions(courseId: string): Promise<ChatSessionDto[]>;
  createSession(courseId: string, body?: CreateSessionBody): Promise<ChatSessionDto>;
  listMessages(sessionId: string): Promise<ChatMessageDto[]>;
  sendMessage(
    sessionId: string,
    body: SendMessageBody,
    options?: SendMessageOptions,
  ): Promise<ChatMessageDto>;
}
