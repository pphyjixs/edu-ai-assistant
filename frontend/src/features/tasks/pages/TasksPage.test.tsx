/**
 * 「任务」页的组件测试。
 *
 * 渲染**真实路由**（``AppRoutes``）+ 真实布局，只把 ``@/services/http`` 换成
 * 按路径返回固定数据的桩，因此「左栏导航里还有没有学习空间」「点芯片之后列表
 * 真的少了一行」这类断言检验的是真实页面行为，而不是替身组件。
 */

import { screen, waitFor, within } from "@testing-library/react";
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
import {
  COURSE_ID,
  COURSE_ID_2,
  COURSE_SUMMARY,
  COURSE_SUMMARY_2,
  PROFILE,
  STUDENT_PROFILE,
  makeAssignment,
  page,
} from "@/test/fixtures";
import { installHttpRoutes, type HttpRoute } from "@/test/httpMock";
import { renderApp, resetBuddyStore, seedTokens } from "@/test/render";
import { formatFullDateTime } from "@/utils/datetime";

const ASSIGNMENTS_ROUTE = /^\/courses\/([0-9a-f-]+)\/assignments$/;

const UPCOMING_ID = "aaaaaaa1-0000-0000-0000-000000000001";
const EXPIRED_ID = "aaaaaaa2-0000-0000-0000-000000000002";
const OTHER_ID = "aaaaaaa3-0000-0000-0000-000000000003";
const DRAFT_ID = "aaaaaaa4-0000-0000-0000-000000000004";

const UPCOMING_DUE = "2026-10-09T15:59:00Z";
const LATER_DUE = "2026-11-01T15:59:00Z";
const EXPIRED_DUE = "2020-01-01T00:00:00Z";

function routes(options: {
  profile?: unknown;
  courses?: unknown[];
  assignments?: Record<string, unknown[]>;
}): HttpRoute[] {
  const { profile = STUDENT_PROFILE, courses = [], assignments = {} } = options;
  return [
    ["/users/me", () => profile],
    ["/courses", () => page(courses, 50)],
    [
      ASSIGNMENTS_ROUTE,
      ({ path }) => {
        const courseId = ASSIGNMENTS_ROUTE.exec(path)?.[1] ?? "";
        return page(assignments[courseId] ?? []);
      },
    ],
  ];
}

/**
 * 学生场景：两门课共三条任务。
 *
 * 草稿**不出现**在学生视角里 —— 真实后端在查询层就排除了草稿（契约 8.1），
 * 桩也必须照这个语义返回，否则测试会验证一个线上不存在的组合。
 */
function studentScenario() {
  return routes({
    profile: STUDENT_PROFILE,
    courses: [COURSE_SUMMARY, COURSE_SUMMARY_2],
    assignments: {
      [COURSE_ID]: [
        makeAssignment({
          id: UPCOMING_ID,
          title: "实验二：浮点加法器设计实验",
          due_at: UPCOMING_DUE,
        }),
        makeAssignment({ id: EXPIRED_ID, title: "第一章作业", due_at: EXPIRED_DUE }),
      ],
      [COURSE_ID_2]: [
        makeAssignment({
          id: OTHER_ID,
          course_id: COURSE_ID_2,
          title: "进程调度实验",
          due_at: LATER_DUE,
        }),
      ],
    },
  });
}

function renderTasks(
  httpRoutes: HttpRoute[] = studentScenario(),
  { route = "/tasks" }: { route?: string } = {},
) {
  installHttpRoutes(httpMock, httpRoutes);
  return renderApp(<AppRoutes />, { route });
}

beforeEach(() => {
  seedTokens();
  resetBuddyStore();
});

describe("任务页", () => {
  it("渲染跨课程待办：标题计数、任务行、课程与截止时间", async () => {
    renderTasks();

    // 等第一行渲染出来再断言计数：加载中不显示数字，避免先看到一个假计数
    expect(await screen.findByText("实验二：浮点加法器设计实验")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /待办/ })).toHaveTextContent("（3）");

    expect(screen.getByText("第一章作业")).toBeInTheDocument();
    expect(screen.getByText("进程调度实验")).toBeInTheDocument();

    // 状态标签按角色翻译：学生看到「待提交」/「已截止」
    // 注意断言要限定在列表内：状态下拉的 option 文案与行内标签是同一套文案
    const list = screen.getByRole("list");
    expect(within(list).getAllByText("待提交")).toHaveLength(2);
    expect(within(list).getByText("已截止")).toBeInTheDocument();

    // 截止时间用完整的「年.月.日 时:分」，跨课程时不会只剩月日
    expect(screen.getByText(formatFullDateTime(UPCOMING_DUE))).toBeInTheDocument();

    // 每行是指向课程内作业详情的链接
    expect(screen.getByRole("link", { name: /实验二/ })).toHaveAttribute(
      "href",
      `/courses/${COURSE_ID}/assignments/${UPCOMING_ID}`,
    );
  });

  it("可动手的排在前面，已逾期的沉到后面", async () => {
    renderTasks();
    await screen.findByText("实验二：浮点加法器设计实验");

    const titles = screen
      .getAllByRole("link")
      .map((link) => link.textContent ?? "")
      .filter((text) => text.includes("实验二") || text.includes("第一章"));

    expect(titles[0]).toContain("实验二");
    expect(titles[1]).toContain("第一章");
  });

  it("课程芯片按课程筛选并标记选中态", async () => {
    renderTasks();
    await screen.findByText("实验二：浮点加法器设计实验");

    expect(screen.getByRole("button", { name: "全部" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    await userEvent.click(screen.getByRole("button", { name: /操作系统/ }));

    expect(screen.queryByText("实验二：浮点加法器设计实验")).toBeNull();
    expect(screen.getByText("进程调度实验")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /操作系统/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    // 选中课程后标题计数跟着变成该课程的数量
    expect(screen.getByRole("heading", { name: /待办/ })).toHaveTextContent("（1）");
  });

  it("截止时间筛选只剩已逾期的任务", async () => {
    renderTasks();
    await screen.findByText("实验二：浮点加法器设计实验");

    await userEvent.selectOptions(screen.getByLabelText("截止时间"), "expired");

    expect(screen.getByText("第一章作业")).toBeInTheDocument();
    expect(screen.queryByText("实验二：浮点加法器设计实验")).toBeNull();
    expect(screen.queryByText("进程调度实验")).toBeNull();
  });

  it("活动类型筛选可选「实验任务」", async () => {
    renderTasks();
    await screen.findByText("实验二：浮点加法器设计实验");

    await userEvent.selectOptions(screen.getByLabelText("活动类型"), "ASSIGNMENT");

    // 目前只有实验任务这一类，因此筛选后条数不变，但筛选确实生效
    expect(screen.getByRole("heading", { name: /待办/ })).toHaveTextContent("（3）");
  });

  it("状态筛选：学生选「待提交」只剩未逾期的任务", async () => {
    renderTasks();
    await screen.findByText("实验二：浮点加法器设计实验");

    await userEvent.selectOptions(screen.getByLabelText("状态"), "todo");

    expect(screen.getByText("实验二：浮点加法器设计实验")).toBeInTheDocument();
    expect(screen.getByText("进程调度实验")).toBeInTheDocument();
    expect(screen.queryByText("第一章作业")).toBeNull();
    expect(screen.getByRole("heading", { name: /待办/ })).toHaveTextContent("（2）");
  });

  it("状态选项随角色变化：学生没有「待发布」", async () => {
    renderTasks();
    await screen.findByText("实验二：浮点加法器设计实验");

    const select = screen.getByLabelText("状态");
    expect(within(select).queryByRole("option", { name: "待发布" })).toBeNull();
    expect(within(select).getByRole("option", { name: "待提交" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "已截止" })).toBeInTheDocument();
  });

  it("状态选项随角色变化：教师看到「待发布」与「进行中」", async () => {
    renderTasks(
      routes({
        profile: PROFILE,
        courses: [COURSE_SUMMARY],
        assignments: {
          [COURSE_ID]: [
            makeAssignment({
              id: DRAFT_ID,
              title: "尚未发布的实验",
              status: "DRAFT",
              due_at: null,
            }),
          ],
        },
      }),
    );

    await screen.findByText("尚未发布的实验");
    const select = screen.getByLabelText("状态");
    expect(within(select).getByRole("option", { name: "待发布" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "进行中" })).toBeInTheDocument();
    expect(within(select).queryByRole("option", { name: "待提交" })).toBeNull();
  });

  it("教师按「待发布」筛选只剩草稿任务", async () => {
    renderTasks(
      routes({
        profile: PROFILE,
        courses: [COURSE_SUMMARY],
        assignments: {
          [COURSE_ID]: [
            makeAssignment({
              id: DRAFT_ID,
              title: "尚未发布的实验",
              status: "DRAFT",
              due_at: null,
            }),
            makeAssignment({ id: UPCOMING_ID, title: "已发布的实验", due_at: UPCOMING_DUE }),
          ],
        },
      }),
    );

    await screen.findByText("尚未发布的实验");
    expect(screen.getByRole("heading", { name: /待办/ })).toHaveTextContent("（2）");

    await userEvent.selectOptions(screen.getByLabelText("状态"), "draft");

    expect(screen.getByText("尚未发布的实验")).toBeInTheDocument();
    expect(screen.queryByText("已发布的实验")).toBeNull();
    expect(screen.getByRole("heading", { name: /待办/ })).toHaveTextContent("（1）");
  });

  it("筛选后为空时给出可操作的提示，清除筛选会重置全部三个条件", async () => {
    renderTasks();
    await screen.findByText("实验二：浮点加法器设计实验");

    // 学生没有草稿，因此「待提交」+「未设置截止」必然为空
    await userEvent.selectOptions(screen.getByLabelText("状态"), "todo");
    await userEvent.selectOptions(screen.getByLabelText("截止时间"), "none");

    expect(await screen.findByText("没有符合筛选条件的任务")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "清除筛选" }));
    expect(await screen.findByText("实验二：浮点加法器设计实验")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /待办/ })).toHaveTextContent("（3）");
    // 三个下拉都回到「全部」
    expect(screen.getByLabelText("状态")).toHaveValue("all");
    expect(screen.getByLabelText("截止时间")).toHaveValue("all");
    expect(screen.getByLabelText("活动类型")).toHaveValue("all");
  });

  it("教师看到的是自己课程的任务，草稿显示为待发布", async () => {
    renderTasks(
      routes({
        profile: PROFILE,
        courses: [COURSE_SUMMARY],
        assignments: {
          [COURSE_ID]: [
            makeAssignment({
              id: DRAFT_ID,
              title: "尚未发布的实验",
              status: "DRAFT",
              due_at: null,
            }),
          ],
        },
      }),
    );

    expect(await screen.findByText("尚未发布的实验")).toBeInTheDocument();
    const list = screen.getByRole("list");
    expect(within(list).getByText("待发布")).toBeInTheDocument();
    expect(within(list).getByText("未设置")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /待办/ })).toHaveTextContent("（1）");
  });

  it("行内信息包含课程名，便于跨课程时区分", async () => {
    renderTasks();
    const row = (await screen.findByRole("link", { name: /实验二/ })) as HTMLElement;

    expect(within(row).getByText("数据库原理")).toBeInTheDocument();
    expect(within(row).getByText("课程：")).toBeInTheDocument();
  });
});

describe("左栏导航", () => {
  it("不再出现「学习空间」入口", async () => {
    renderTasks();
    await screen.findByRole("heading", { name: /待办/ });

    const nav = screen.getByRole("navigation", { name: "主导航" });
    expect(within(nav).queryByText("学习空间")).toBeNull();
    // 首页 / 我的课程 / 任务 三个入口保留
    expect(within(nav).getByRole("link", { name: "首页" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "我的课程" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "任务" })).toBeInTheDocument();
  });

  it("/workspace 不再可达（回落到 404 而不是白屏）", async () => {
    renderTasks(studentScenario(), { route: "/workspace" });

    await waitFor(() => {
      expect(screen.getByText("页面不存在")).toBeInTheDocument();
    });
  });
});
