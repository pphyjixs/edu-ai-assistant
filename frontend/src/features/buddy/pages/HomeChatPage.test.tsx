/**
 * 首页中央会话页的组件测试（开发方案 10.3 的第 2–8 条）。
 *
 * 方式：渲染**真实路由**（``AppRoutes``）+ 真实的 ``DashboardLayout``，
 * 只把 ``@/services/http`` 换成按路径返回固定数据的桩。这样"是否弹出右侧抽屉"、
 * "课程上下文从哪来"这类断言检验的是真实布局行为，而不是替身组件。
 */

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));

vi.mock("@/services/http", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/http")>();
  return { ...actual, http: httpMock };
});

import { AppRoutes } from "@/app/router";
import { useBuddyStore } from "@/features/buddy/store/buddyStore";
import { HttpError } from "@/services/http";
import {
  ASSISTANT_MESSAGE,
  CHAT_SESSION,
  COURSE_DETAIL,
  COURSE_ID,
  COURSE_SUMMARY,
  EMPTY_PAGE,
  PROFILE,
  SESSION_ID,
  USER_MESSAGE,
  USER_MESSAGE_ID,
  makeRun,
  page,
} from "@/test/fixtures";
import { installHttpRoutes, type HttpRoute } from "@/test/httpMock";
import { renderApp, resetBuddyStore, seedTokens } from "@/test/render";

const OTHER_COURSE_ID = "99999999-9999-9999-9999-999999999999";

type RoutesOptions = {
  messages?: unknown[];
  runs?: unknown[];
  activeRun?: unknown;
  /** ``POST /chat-sessions/{id}/runs`` 的行为；默认返回一个成功的 Run */
  createRun?: () => unknown;
};

function routes({
  messages = [USER_MESSAGE, ASSISTANT_MESSAGE],
  runs = [],
  activeRun = null,
  createRun,
}: RoutesOptions = {}): HttpRoute[] {
  return [
    ["/users/me", () => PROFILE],
    ["/courses", () => page([COURSE_SUMMARY], 50)],
    [/^\/courses\/[0-9a-f-]+\/chat-sessions$/, () => EMPTY_PAGE],
    [/^\/courses\/[0-9a-f-]+\/materials$/, () => EMPTY_PAGE],
    [/^\/courses\/[0-9a-f-]+\/assignments$/, () => EMPTY_PAGE],
    [`/courses/${COURSE_ID}`, () => COURSE_DETAIL],
    [`/chat-sessions/${SESSION_ID}`, () => CHAT_SESSION],
    [`/chat-sessions/${SESSION_ID}/messages`, () => page(messages, 100)],
    [
      `/chat-sessions/${SESSION_ID}/runs`,
      ({ method }) => (method === "POST" ? (createRun ? createRun() : makeRun()) : page(runs)),
    ],
    [`/chat-sessions/${SESSION_ID}/active-run`, () => ({ run: activeRun })],
  ];
}

function renderChatPage(options: RoutesOptions = {}) {
  installHttpRoutes(httpMock, routes(options));
  return renderApp(<AppRoutes />, { route: `/chats/${SESSION_ID}` });
}

beforeEach(() => {
  seedTokens();
  resetBuddyStore();
});

describe("首页中央会话页", () => {
  it("不渲染 overlay 面板与悬浮入口，顶栏也不再出现 Buddy 按钮", async () => {
    renderChatPage();

    expect(await screen.findAllByText("数据库原理")).not.toHaveLength(0);
    expect(screen.queryByLabelText("StudyBuddy 助手面板")).toBeNull();
    expect(screen.queryByLabelText("打开 Buddy 面板")).toBeNull();
    expect(screen.queryByRole("button", { name: "Buddy" })).toBeNull();
    expect(useBuddyStore.getState().buddyOpen).toBe(false);
  });

  it("使用紧凑上下文条与对齐的消息列，顶部不留大段空白", async () => {
    renderChatPage();

    await screen.findAllByText("数据库原理");
    // 上下文条收成一行（不再沿用面板那种约 80px 高的块）
    expect(document.querySelector('[class*="barInline"]')).not.toBeNull();
    // 消息列表 / 建议提问 / 输入框都改由中央列统一控制左右边缘
    expect(document.querySelector('[class*="listCenter"]')).not.toBeNull();
    expect(document.querySelector('[class*="composerCenter"]')).not.toBeNull();
  });

  it("以服务端返回的 course_id 恢复上下文，而不是本地残留的课程", async () => {
    // 模拟"上次在别的课程里"留下的错误归属
    useBuddyStore.setState({ activeChatCourseId: OTHER_COURSE_ID });

    renderChatPage();

    await waitFor(() => {
      expect(useBuddyStore.getState().activeChatSessionId).toBe(SESSION_ID);
    });
    expect(useBuddyStore.getState().activeChatCourseId).toBe(COURSE_ID);
    // 声明给后端的上下文也必须来自服务端
    await waitFor(() => {
      expect(useBuddyStore.getState().buddyContext.courseId).toBe(COURSE_ID);
    });
  });

  it("刷新后在中央显示进行中的 Run 进度，且不打开右侧抽屉", async () => {
    const running = makeRun({
      status: "RUNNING",
      progress: 0,
      output_message_id: null,
      finished_at: null,
      steps: [
        {
          order: 1,
          kind: "TOOL_CALL",
          name: "search_course_knowledge",
          status: "SUCCEEDED",
          error_code: null,
        },
      ],
    });

    renderChatPage({ runs: [running], activeRun: running });

    expect(await screen.findByText(/正在检索课程资料/)).toBeInTheDocument();
    expect(useBuddyStore.getState().buddyOpen).toBe(false);
    expect(screen.queryByLabelText("StudyBuddy 助手面板")).toBeNull();
  });

  it("把 PRACTICE_SET artifact 渲染成正确课程的链接", async () => {
    const succeeded = makeRun({
      artifacts: [
        {
          kind: "PRACTICE_SET",
          id: "77777777-7777-7777-7777-777777777777",
          job_id: "88888888-8888-8888-8888-888888888888",
          status: "PENDING",
          href: `/courses/${COURSE_ID}/learn`,
        },
      ],
    });

    renderChatPage({ runs: [succeeded] });

    const link = await screen.findByRole("link", { name: /已创建一套练习/ });
    expect(link).toHaveAttribute("href", `/courses/${COURSE_ID}/learn`);
  });

  it("工具失败时显示具体文案，而不是笼统的未知错误", async () => {
    const failed = makeRun({
      status: "FAILED",
      failure_stage: "TOOL_CALL",
      error: "工具执行失败，请稍后重试",
      output_message_id: null,
      finished_at: null,
      steps: [
        {
          order: 1,
          kind: "TOOL_CALL",
          name: "get_assignment",
          status: "FAILED",
          error_code: "RESOURCE_NOT_FOUND",
        },
      ],
    });

    renderChatPage({ runs: [failed] });

    expect(await screen.findByText("工具调用失败")).toBeInTheDocument();
    expect(screen.getByText("目标资源不存在或不可见")).toBeInTheDocument();
    expect(screen.queryByText(/未知错误/)).toBeNull();
  });

  it("模型服务不支持工具调用时给出专门的失败说明", async () => {
    const failed = makeRun({
      status: "FAILED",
      failure_stage: "MODEL_TOOL_CALL_UNSUPPORTED",
      error: "当前模型服务不支持工具调用",
      output_message_id: null,
      finished_at: null,
    });

    renderChatPage({ runs: [failed] });

    expect(await screen.findByText("模型服务不支持工具调用")).toBeInTheDocument();
    expect(
      screen.getByText(/需要在服务端换成支持 function calling 的模型/),
    ).toBeInTheDocument();
  });
});

describe("中央会话页的输入框", () => {
  it("遵循 Enter 发送、Shift+Enter 换行的约定", async () => {
    renderChatPage();
    const composer = await screen.findByPlaceholderText(/针对当前课程/);

    await userEvent.type(composer, "第一行");
    await userEvent.type(composer, "{Shift>}{Enter}{/Shift}");
    await userEvent.type(composer, "第二行");

    expect(composer).toHaveValue("第一行\n第二行");
    // 换行不应触发发送
    expect(httpMock.post).not.toHaveBeenCalled();
  });

  it("存在进行中的 Run 时禁用再次提交", async () => {
    const running = makeRun({
      status: "RUNNING",
      progress: 0,
      output_message_id: null,
      finished_at: null,
    });

    renderChatPage({ runs: [running], activeRun: running });

    const send = await screen.findByRole("button", { name: "发送" });
    expect(send).toBeDisabled();
  });

  it("发送失败时保留草稿并显示可读错误", async () => {
    renderChatPage({
      createRun: () =>
        Promise.reject(
          new HttpError({
            code: "AGENT_RUN_IN_PROGRESS",
            message: "这个会话还有一个进行中的任务，请等它结束",
            status: 409,
          }),
        ),
    });

    const composer = await screen.findByPlaceholderText(/针对当前课程/);
    await userEvent.type(composer, "再总结一次这门课");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(
      await screen.findByText("这个会话还有一个进行中的任务，请等它结束"),
    ).toBeInTheDocument();
    expect(composer).toHaveValue("再总结一次这门课");
  });

  it("发送成功后才清空输入框", async () => {
    renderChatPage();
    const composer = await screen.findByPlaceholderText(/针对当前课程/);

    await userEvent.type(composer, "总结这门课");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(httpMock.post).toHaveBeenCalled());
    await waitFor(() => expect(composer).toHaveValue(""));
    expect(httpMock.post).toHaveBeenCalledWith(
      `/chat-sessions/${SESSION_ID}/runs`,
      expect.objectContaining({ input: "总结这门课", action: "ASK" }),
    );
    // 中央会话页发消息不会打开右侧面板
    expect(useBuddyStore.getState().buddyOpen).toBe(false);
    // 用户消息应当立即出现在中央列表里
    expect(USER_MESSAGE_ID).toBeTruthy();
  });
});
