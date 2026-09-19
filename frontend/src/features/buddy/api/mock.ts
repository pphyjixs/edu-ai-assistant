/**
 * Buddy 的 feature 级 mock adapter。
 *
 * 后端 learning 模块尚未实现（backend/app/modules/learning 只有占位文件），
 * 因此这一阶段用内存 mock 打通交互，并刻意覆盖两种回答形态：
 * - 有依据：返回 grounded=true 与契约 6 的 citations；
 * - 无依据：返回 grounded=false 并明确说明「课程资料中未找到依据」
 *   （docs/acceptance.md 第 5 节的要求）。
 *
 * 换真实接口时只替换本文件，页面与组件不需要改动。
 */

import type {
  BuddyApi,
  ChatMessageDto,
  ChatSessionDto,
  CitationDto,
  CreateSessionBody,
  SendMessageBody,
  SendMessageOptions,
} from "./contracts";

/** 模拟网络往返，让 Loading 态在验收时看得见 */
const LIST_DELAY_MS = 420;
const SEND_DELAY_MS = 780;

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function uuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `id-${Math.random().toString(16).slice(2)}-${Date.now().toString(16)}`;
}

function nowIso(): string {
  return new Date().toISOString();
}

const sessionsByCourse = new Map<string, ChatSessionDto[]>();
const messagesBySession = new Map<string, ChatMessageDto[]>();
let sessionCounter = 0;

const ASSIGNMENT_CITATIONS: CitationDto[] = [
  {
    material_id: "3f1c1f4e-7a2b-4a55-9d21-6e0f5a1b7c10",
    material_name: "实验二要求.pdf",
    section_id: "9b0f2d61-1c4a-4f13-8e57-2d6a4c8b9e01",
    section_title: "二、实验要求",
    page: 1,
    quote: "实现基础的多头注意力模块，并说明张量维度变化。",
  },
  {
    material_id: "3f1c1f4e-7a2b-4a55-9d21-6e0f5a1b7c10",
    material_name: "实验二要求.pdf",
    section_id: "c4a7e219-58b3-4c0d-9f6a-71b2d5c3a804",
    section_title: "三、评分标准",
    page: 2,
    quote: "实验报告必须包含模型结构说明、关键代码、实验结果截图以及不少于 300 字的结果分析。",
  },
];

/** 没有具体作业上下文时（例如首页提问）用的课程级引用 */
const COURSE_CITATIONS: CitationDto[] = [
  {
    material_id: "5a2d3e60-8b1c-4d27-a4f9-3c5e7a9b0d21",
    material_name: "03 神经网络与注意力.pdf",
    section_id: "7e1b0c94-3a52-4b86-9d17-8f0a2c4e6b13",
    section_title: "3.4 注意力机制",
    page: 12,
    quote: "注意力机制通过 Query 与 Key 的相似度决定 Value 的加权方式。",
  },
];

/**
 * 带外上下文是不透明的，adapter 在这里做一次窄化。
 * 线上实现会把它映射成真正的请求参数；mock 只用它选取更贴近的引用。
 */
function entityTypeOf(context: unknown): string | undefined {
  if (context && typeof context === "object" && "entityType" in context) {
    const value = (context as { entityType?: unknown }).entityType;
    return typeof value === "string" ? value : undefined;
  }
  return undefined;
}

type CannedAnswer = {
  content: string;
  citations: CitationDto[];
};

/** 命中关键词时返回有依据的回答；未命中则走「资料不足」分支 */
function answerFor(question: string, citations: CitationDto[]): CannedAnswer | null {
  const text = question.toLowerCase();

  if (text.includes("拆解") || text.includes("步骤") || text.includes("完成计划")) {
    return {
      content: [
        "按实验要求，可以拆成四步推进：",
        "",
        "1. 数据准备：完成数据读取与预处理模块，先确认样本数量与标签分布。",
        "2. 核心实现：实现基础的多头注意力模块，并把 Q、K、V 到输出每一步的张量维度写清楚。",
        "3. 对照实验：训练文本分类模型，对比至少两组参数设置（例如头数或学习率）。",
        "4. 结果分析：整理实验数据，分析参数变化对效果的影响，不少于 300 字。",
        "",
        "提醒：报告里必须包含模型结构说明、关键代码、结果截图和分析段落，这四项直接对应评分标准。",
      ].join("\n"),
      citations,
    };
  }

  if (text.includes("总结") || text.includes("要求")) {
    return {
      content: [
        "这份实验的核心是「理解 Self-Attention 并跑通一个最小可用的文本分类实验」。",
        "",
        "要求分四块：预处理、多头注意力实现（要说明维度变化）、至少两组参数的对照实验、以及结果分析。报告里要有结构说明、关键代码、结果截图和 300 字以上的分析。",
      ].join("\n"),
      citations,
    };
  }

  if (text.includes("易错") || text.includes("出错") || text.includes("注意")) {
    return {
      content: [
        "几处容易丢分的地方：",
        "",
        "1. 张量维度只在代码里出现、却没有文字说明——评分标准明确要求说明维度变化。",
        "2. 只跑了一组参数就下结论，缺少对照，分析段落会站不住。",
        "3. 结果截图没有标注实验条件，无法判断是哪一组参数得到的。",
        "4. 分析不足 300 字，或者只是复述步骤而没有解释原因。",
      ].join("\n"),
      citations,
    };
  }

  if (text.includes("检查") || text.includes("报告")) {
    return {
      content: [
        "我会按评分项逐条核对：模型结构说明、关键代码、实验结果截图、分析段落是否齐全，以及是否体现了至少两组参数的对比。",
        "",
        "把报告传上来之后，我可以指出缺哪一项——但请注意这只是提交前的自查，不等于教师给出的正式成绩。",
      ].join("\n"),
      citations,
    };
  }

  return null;
}

export const mockBuddyApi: BuddyApi = {
  async listSessions(courseId: string): Promise<ChatSessionDto[]> {
    await delay(LIST_DELAY_MS);
    return sessionsByCourse.get(courseId) ?? [];
  },

  async createSession(courseId: string, body?: CreateSessionBody): Promise<ChatSessionDto> {
    await delay(LIST_DELAY_MS);
    sessionCounter += 1;
    const session: ChatSessionDto = {
      id: uuid(),
      course_id: courseId,
      title: body?.title ?? `新对话 ${sessionCounter}`,
      created_at: nowIso(),
    };
    const existing = sessionsByCourse.get(courseId) ?? [];
    sessionsByCourse.set(courseId, [session, ...existing]);
    messagesBySession.set(session.id, []);
    return session;
  },

  async listMessages(sessionId: string): Promise<ChatMessageDto[]> {
    await delay(LIST_DELAY_MS);
    return messagesBySession.get(sessionId) ?? [];
  },

  async sendMessage(
    sessionId: string,
    body: SendMessageBody,
    options?: SendMessageOptions,
  ): Promise<ChatMessageDto> {
    await delay(SEND_DELAY_MS);

    const history = messagesBySession.get(sessionId) ?? [];
    const userMessage: ChatMessageDto = {
      id: uuid(),
      role: "USER",
      // 只存用户输入的原文：带外上下文不进入可见消息
      content: body.content,
      grounded: false,
      citations: [],
      created_at: nowIso(),
    };

    // 根据带外上下文挑选更贴近的引用来源
    const citations =
      entityTypeOf(options?.context) === "assignment" ? ASSIGNMENT_CITATIONS : COURSE_CITATIONS;

    const canned = answerFor(body.content, citations);
    const assistantMessage: ChatMessageDto = {
      id: uuid(),
      role: "ASSISTANT",
      content: canned
        ? canned.content
        : "课程资料中未找到依据，因此我不做推测。可以换一种问法，或者等教师上传对应章节的资料后再问。",
      grounded: Boolean(canned),
      citations: canned ? canned.citations : [],
      created_at: nowIso(),
    };

    messagesBySession.set(sessionId, [...history, userMessage, assistantMessage]);
    return userMessage;
  },
};
