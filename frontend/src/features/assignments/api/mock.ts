/**
 * Assignments 的 feature 级 mock adapter。
 *
 * 截止时间按「当前时间」相对生成，保证任何一天打开演示都是合理状态
 * （一条临近截止、一条已截止、一条还有一周）。
 * 课程 id 与 courses mock 保持一致（演示种子数据）。
 */

import { HttpError } from "@/services/http";

import type {
  AssignmentDto,
  AssignmentsApi,
  CreateSubmissionBody,
  SubmissionDto,
} from "./contracts";

const LIST_DELAY_MS = 360;
const SUBMIT_DELAY_MS = 900;

/** 与 courses/api/mock.ts 的 MOCK_COURSE_IDS 保持一致 */
const COURSE_AI = "b1a7c3e2-4d58-4f90-9c21-6e7a8b0d1f34";
const COURSE_DATABASE = "c2b8d4f3-5e69-4a01-8d32-7f8b9c1e2a45";
const CURRENT_USER_ID = "8c1f4b1e-2a3d-4c5e-9f60-7b8d9e0a1c22";

const DAY_MS = 24 * 60 * 60 * 1000;

function isoFromNow(deltaMs: number): string {
  return new Date(Date.now() + deltaMs).toISOString();
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

const ASSIGNMENTS: AssignmentDto[] = [
  {
    id: "a7f3d1b2-9c4e-4a6f-8b1d-2e3f4a5b6c70",
    course_id: COURSE_AI,
    title: "实验二：Transformer 文本分类实验",
    description: [
      "一、实验目标",
      "理解 Transformer 中 Self-Attention 的基本结构，掌握 Query、Key、Value 的作用，并能够完成一个简化版文本分类模型。",
      "",
      "二、实验要求",
      "1. 完成数据读取和预处理模块。",
      "2. 实现基础的多头注意力模块，并说明张量维度变化。",
      "3. 训练文本分类模型，对比至少两组参数设置。",
      "4. 整理实验结果，分析参数变化对模型效果的影响。",
      "",
      "三、报告要求",
      "实验报告必须包含模型结构说明、关键代码、实验结果截图，以及不少于 300 字的结果分析。",
    ].join("\n"),
    total_score: 100,
    due_at: isoFromNow(DAY_MS),
    allow_late_submission: false,
    status: "PUBLISHED",
    rubric_items: [
      {
        id: "r-0001-0000-4000-8000-000000000001",
        title: "代码实现",
        description: "多头注意力模块是否正确实现，张量维度说明是否清晰。",
        max_score: 30,
        order: 1,
      },
      {
        id: "r-0001-0000-4000-8000-000000000002",
        title: "实验结果",
        description: "是否完成至少两组参数设置的对照实验，结果是否可复现。",
        max_score: 25,
        order: 2,
      },
      {
        id: "r-0001-0000-4000-8000-000000000003",
        title: "分析讨论",
        description: "是否解释参数变化对效果的影响，分析不少于 300 字。",
        max_score: 30,
        order: 3,
      },
      {
        id: "r-0001-0000-4000-8000-000000000004",
        title: "报告规范",
        description: "结构完整、图表标注清晰、引用规范。",
        max_score: 15,
        order: 4,
      },
    ],
    created_at: isoFromNow(-9 * DAY_MS),
  },
  {
    id: "b8e4c2d3-0d5f-4b70-9c2e-3f4a5b6c7d81",
    course_id: COURSE_AI,
    title: "实验一：需求分析与用例建模",
    description: [
      "一、实验目标",
      "掌握面向对象需求分析的基本方法，能够把自然语言需求转化为用例模型。",
      "",
      "二、实验要求",
      "1. 选择一个熟悉的信息系统，梳理功能与非功能需求。",
      "2. 绘制用例图，并说明参与者与用例之间的关系。",
      "3. 对其中一个核心用例编写详细的用例描述。",
    ].join("\n"),
    total_score: 100,
    due_at: isoFromNow(-6 * DAY_MS),
    allow_late_submission: false,
    status: "CLOSED",
    rubric_items: [
      {
        id: "r-0002-0000-4000-8000-000000000001",
        title: "需求完整性",
        description: "功能需求与非功能需求是否覆盖完整。",
        max_score: 40,
        order: 1,
      },
      {
        id: "r-0002-0000-4000-8000-000000000002",
        title: "建模规范",
        description: "用例图与文字描述是否一致，符号使用是否规范。",
        max_score: 60,
        order: 2,
      },
    ],
    created_at: isoFromNow(-20 * DAY_MS),
  },
  {
    id: "c9f5d3e4-1e60-4c81-8d3f-4a5b6c7d8e92",
    course_id: COURSE_AI,
    title: "实验三：注意力可视化与误差分析",
    description: [
      "一、实验目标",
      "在实验二的基础上，对训练好的模型做注意力权重可视化，并分析典型错误样本。",
      "",
      "二、实验要求",
      "1. 选取至少 3 个测试样本，输出注意力权重热力图。",
      "2. 找出至少 2 个分类错误样本，分析可能的失败原因。",
    ].join("\n"),
    total_score: 100,
    due_at: isoFromNow(8 * DAY_MS),
    allow_late_submission: true,
    status: "PUBLISHED",
    rubric_items: [
      {
        id: "r-0003-0000-4000-8000-000000000001",
        title: "可视化质量",
        description: "热力图是否清晰，样本是否具有代表性。",
        max_score: 50,
        order: 1,
      },
      {
        id: "r-0003-0000-4000-8000-000000000002",
        title: "误差分析",
        description: "失败原因分析是否合理且有证据支撑。",
        max_score: 50,
        order: 2,
      },
    ],
    created_at: isoFromNow(-2 * DAY_MS),
  },
  {
    id: "d1a6e4f5-2f71-4d92-9e40-5b6c7d8e9fa3",
    course_id: COURSE_DATABASE,
    title: "SQL 多表查询练习",
    description: [
      "一、练习目标",
      "熟练使用 JOIN、GROUP BY 与子查询完成多表数据检索。",
      "",
      "二、练习要求",
      "1. 基于给定的选课数据库，写出 8 条查询语句。",
      "2. 至少包含两次多表连接与一次分组聚合。",
      "3. 对每条语句给出执行结果的文字说明。",
    ].join("\n"),
    total_score: 100,
    due_at: isoFromNow(3 * DAY_MS),
    allow_late_submission: true,
    status: "PUBLISHED",
    rubric_items: [
      {
        id: "r-0004-0000-4000-8000-000000000001",
        title: "查询正确性",
        description: "语句是否返回符合题目要求的结果。",
        max_score: 60,
        order: 1,
      },
      {
        id: "r-0004-0000-4000-8000-000000000002",
        title: "语句质量",
        description: "是否使用合适的连接方式，可读性是否良好。",
        max_score: 40,
        order: 2,
      },
    ],
    created_at: isoFromNow(-5 * DAY_MS),
  },
];

/** assignment_id → 本人的提交 */
const submissions = new Map<string, SubmissionDto>();

export const mockAssignmentsApi: AssignmentsApi = {
  async listAssignments(courseId: string): Promise<AssignmentDto[]> {
    await delay(LIST_DELAY_MS);
    return ASSIGNMENTS.filter((item) => item.course_id === courseId);
  },

  async getAssignment(assignmentId: string): Promise<AssignmentDto> {
    await delay(LIST_DELAY_MS);
    const found = ASSIGNMENTS.find((item) => item.id === assignmentId);
    if (!found) {
      throw new HttpError({
        code: "RESOURCE_NOT_FOUND",
        message: "作业不存在，或者你没有查看权限。",
        status: 404,
      });
    }
    return found;
  },

  async getMySubmission(assignmentId: string): Promise<SubmissionDto | null> {
    await delay(LIST_DELAY_MS);
    return submissions.get(assignmentId) ?? null;
  },

  async createSubmission(
    assignmentId: string,
    body: CreateSubmissionBody,
  ): Promise<SubmissionDto> {
    await delay(SUBMIT_DELAY_MS);

    const assignment = ASSIGNMENTS.find((item) => item.id === assignmentId);
    if (!assignment) {
      throw new HttpError({
        code: "RESOURCE_NOT_FOUND",
        message: "作业不存在，或者你没有查看权限。",
        status: 404,
      });
    }

    // 契约 12 的稳定错误码：任务未发布或已关闭
    const expired = assignment.due_at !== null && new Date(assignment.due_at) <= new Date();
    if (assignment.status !== "PUBLISHED" || (expired && !assignment.allow_late_submission)) {
      throw new HttpError({
        code: "ASSIGNMENT_NOT_OPEN",
        message: "该作业已关闭提交，如需补交请联系任课教师。",
        status: 409,
      });
    }

    if (!body.filename.trim()) {
      throw new HttpError({
        code: "UPLOAD_INVALID",
        message: "请选择要提交的实验报告文件。",
        status: 422,
      });
    }

    const submission: SubmissionDto = {
      id: `s-${assignmentId.slice(0, 8)}-${Date.now().toString(16)}`,
      assignment_id: assignmentId,
      user_id: CURRENT_USER_ID,
      status: "SUBMITTED",
      created_at: new Date().toISOString(),
    };

    submissions.set(assignmentId, submission);
    return submission;
  },
};
