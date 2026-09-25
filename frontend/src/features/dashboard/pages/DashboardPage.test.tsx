/**
 * 首页（空态）的组件测试（开发方案 10.3 的第 1、2、6 条）。
 *
 * 关键验收点：从首页中央输入框发送后，**创建会话与 Run、进入 /chats/:sessionId**，
 * 而且整个过程**不打开右侧抽屉**（buddyOpen 保持 false）。
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
  CHAT_SESSION,
  COURSE_DETAIL,
  COURSE_ID,
  COURSE_SUMMARY,
  EMPTY_PAGE,
  PROFILE,
  SESSION_ID,
  makeRun,
  page,
} from "@/test/fixtures";
import { installHttpRoutes, type HttpRoute, type HttpRouteRequest } from "@/test/httpMock";
import { renderApp, resetBuddyStore, seedTokens } from "@/test/render";

type RoutesOptions = {
  createSession?: () => unknown;
  createRun?: () => unknown;
};

function routes({ createSession, createRun }: RoutesOptions = {}): HttpRoute[] {
  return [
    ["/users/me", () => PROFILE],
    ["/courses", () => page([COURSE_SUMMARY], 50)],
    [`/courses/${COURSE_ID}`, () => COURSE_DETAIL],
    [
      `/courses/${COURSE_ID}/chat-sessions`,
      ({ method }: HttpRouteRequest) =>
        method === "POST" ? (createSession ? createSession() : CHAT_SESSION) : EMPTY_PAGE,
    ],
    [/^\/courses\/[0-9a-f-]+\/assignments$/, () => EMPTY_PAGE],
    [`/chat-sessions/${SESSION_ID}`, () => CHAT_SESSION],
    [`/chat-sessions/${SESSION_ID}/messages`, () => page([])],
    [
      `/chat-sessions/${SESSION_ID}/runs`,
      ({ method }: HttpRouteRequest) =>
        method === "POST" ? (createRun ? createRun() : makeRun()) : page([]),
    ],
    [`/chat-sessions/${SESSION_ID}/active-run`, () => ({ run: null })],
  ];
}

function renderHome(options: RoutesOptions = {}) {
  installHttpRoutes(httpMock, routes(options));
  return renderApp(<AppRoutes />, { route: "/" });
}

async function typeAndSend(text: string) {
  const input = await screen.findByLabelText("向 Buddy 提问");
  await userEvent.type(input, text);
  await userEvent.click(screen.getByRole("button", { name: "发送" }));
  return input;
}

beforeEach(() => {
  seedTokens();
  resetBuddyStore();
});

describe("首页发送第一条消息", () => {
  it("创建会话与 Run，并导航到 /chats/:sessionId", async () => {
    renderHome();

    await typeAndSend("帮我拆解实验二的任务");

    await waitFor(() =>
      expect(httpMock.post).toHaveBeenCalledWith(
        `/courses/${COURSE_ID}/chat-sessions`,
        {},
      ),
    );
    await waitFor(() =>
      expect(httpMock.post).toHaveBeenCalledWith(
        `/chat-sessions/${SESSION_ID}/runs`,
        expect.objectContaining({ input: "帮我拆解实验二的任务", action: "ASK" }),
      ),
    );

    // 导航到中央会话页的证据：页面开始加载该会话
    await waitFor(() =>
      expect(httpMock.get).toHaveBeenCalledWith(
        `/chat-sessions/${SESSION_ID}`,
        expect.anything(),
      ),
    );
  });

  it("发送首页消息不会打开右侧面板", async () => {
    renderHome();

    await typeAndSend("总结这门课");

    await waitFor(() => expect(httpMock.post).toHaveBeenCalled());
    expect(useBuddyStore.getState().buddyOpen).toBe(false);
    // 首页空态下的 overlay 面板与悬浮入口都不应因为发送而出现
    expect(screen.queryByLabelText("StudyBuddy 助手面板")).toBeNull();
  });

  it("首页右上角不再出现 Buddy 入口（功能与中央输入框重复）", async () => {
    renderHome();

    await screen.findByLabelText("向 Buddy 提问");
    expect(screen.queryByRole("button", { name: "Buddy" })).toBeNull();
    expect(screen.queryByLabelText("打开 Buddy 面板")).toBeNull();
  });

  it("创建 Run 失败时保留输入内容并显示错误", async () => {
    renderHome({
      createRun: () =>
        Promise.reject(
          new HttpError({
            code: "AGENT_RUN_IN_PROGRESS",
            message: "这个会话还有一个进行中的任务，请等它结束",
            status: 409,
          }),
        ),
    });

    const input = await typeAndSend("总结这门课");

    expect(
      await screen.findByText("这个会话还有一个进行中的任务，请等它结束"),
    ).toBeInTheDocument();
    // 草稿必须保留，用户改一改就能重发
    expect(input).toHaveValue("总结这门课");
    expect(useBuddyStore.getState().buddyOpen).toBe(false);
  });

  it("没有课程上下文时给出可读错误，而不是静默失败", async () => {
    // 课程列表为空：契约 6 的会话按课程创建，没有课程就无法提问
    installHttpRoutes(httpMock, [
      ["/users/me", () => PROFILE],
      ["/courses", () => page([])],
      [/^\/courses\/[0-9a-f-]+\/assignments$/, () => EMPTY_PAGE],
    ]);

    renderApp(<AppRoutes />, { route: "/" });

    const input = await screen.findByLabelText("向 Buddy 提问");
    await userEvent.type(input, "在吗");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(
      await screen.findByText("请先选择要提问的课程，再向 Buddy 提问。"),
    ).toBeInTheDocument();
    expect(httpMock.post).not.toHaveBeenCalled();
  });

  it("新建对话回到首页后保留最近选择的课程", async () => {
    useBuddyStore.setState({ activeChatCourseId: COURSE_ID, activeChatSessionId: SESSION_ID });

    renderHome();

    // 「新建对话」只清会话、不清课程，首页据此预选同一门课
    await userEvent.click(await screen.findByRole("button", { name: /新建对话/ }));

    await waitFor(() => {
      expect(useBuddyStore.getState().activeChatSessionId).toBeUndefined();
    });
    expect(useBuddyStore.getState().activeChatCourseId).toBe(COURSE_ID);
    expect(useBuddyStore.getState().buddyOpen).toBe(false);
    // 回到首页空态：中央输入框仍然可用（课程被预选，可以直接继续提问）
    expect(await screen.findByLabelText("向 Buddy 提问")).toBeInTheDocument();
  });
});
