/**
 * Buddy 的 API 入口。
 *
 * 后端 learning 模块尚未实现，因此这里指向 mock 实现。
 * 后端接口就绪后，只需把下面一行换成基于 services/http 的实现，
 * 页面、Hook 和组件都不需要改动。
 */

import type { BuddyContext } from "../model/types";

import type {
  BuddyApi,
  SendMessageBody,
  SendMessageOptions,
} from "./contracts";
import { mockBuddyApi } from "./mock";

export const buddyApi: BuddyApi = mockBuddyApi;

export type {
  BuddyApi,
  ChatMessageDto,
  ChatSessionDto,
  CitationDto,
  SendMessageBody,
  SendMessageOptions,
} from "./contracts";

/**
 * 把「发送一条消息」拆成契约请求体 + 带外上下文。
 *
 * 已知契约缺口：契约 6 的提问请求体只有 ``{ content }``，没有上下文字段，
 * 而产品要求 Buddy 自动知道当前课程 / 作业 / 资料。
 *
 * 处理方式：请求体严格保持契约形状（只有用户输入的原文，
 * 不会把内部 id 拼进用户看得见的消息里），
 * 上下文通过 ``SendMessageOptions`` 交给 adapter。
 * 契约补上可选字段后，只需要改这一个函数。
 */
export function buildSendRequest(
  content: string,
  context: BuddyContext,
): { body: SendMessageBody; options: SendMessageOptions } {
  return {
    body: { content },
    options: { context },
  };
}
