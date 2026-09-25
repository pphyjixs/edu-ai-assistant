/**
 * 组件测试的固定数据。
 *
 * 通过 HTTP 桩返回，因此不需要严格匹配生成的 DTO 类型；这里只保证
 * **应用真正读取的字段**都在，避免测试因为缺字段而走到兜底分支。
 */

export const USER_ID = "33333333-3333-3333-3333-333333333333";
export const COURSE_ID = "22222222-2222-2222-2222-222222222222";
export const SESSION_ID = "11111111-1111-1111-1111-111111111111";
export const USER_MESSAGE_ID = "44444444-4444-4444-4444-444444444444";

export const PROFILE = {
  id: USER_ID,
  email: "teacher@example.com",
  display_name: "王老师",
  role: "TEACHER",
  created_at: "2026-01-01T00:00:00Z",
};

export const COURSE_SUMMARY = {
  id: COURSE_ID,
  name: "数据库原理",
  description: "关系代数与 SQL",
  status: "ACTIVE",
  teacher_id: USER_ID,
  member_count: 12,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
};

export const COURSE_DETAIL = {
  ...COURSE_SUMMARY,
  invite_code: "ABCDEFGHIJKL",
  members: [],
};

export const CHAT_SESSION = {
  id: SESSION_ID,
  course_id: COURSE_ID,
  created_at: "2026-01-03T00:00:00Z",
  last_message_at: "2026-01-03T00:05:00Z",
};

export const USER_MESSAGE = {
  id: USER_MESSAGE_ID,
  session_id: SESSION_ID,
  role: "USER",
  content: "总结这门课",
  grounded: null,
  citations: [],
  created_at: "2026-01-03T00:05:00Z",
};

export const ASSISTANT_MESSAGE = {
  id: "55555555-5555-5555-5555-555555555555",
  session_id: SESSION_ID,
  role: "ASSISTANT",
  content: "这门课主要讲关系代数与 SQL。",
  grounded: true,
  citations: [],
  created_at: "2026-01-03T00:05:01Z",
};

export type RunFixture = {
  id: string;
  session_id: string;
  action: string;
  status: string;
  progress: number;
  input_message_id: string;
  output_message_id: string | null;
  error: string | null;
  failure_stage: string | null;
  evidence_level: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  sources: unknown[];
  steps: unknown[];
  artifacts: unknown[];
};

export function makeRun(overrides: Partial<RunFixture> = {}): RunFixture {
  return {
    id: "66666666-6666-6666-6666-666666666666",
    session_id: SESSION_ID,
    action: "ASK",
    status: "SUCCEEDED",
    progress: 100,
    input_message_id: USER_MESSAGE_ID,
    output_message_id: ASSISTANT_MESSAGE.id,
    error: null,
    failure_stage: null,
    evidence_level: "FULL",
    created_at: "2026-01-03T00:05:00Z",
    started_at: "2026-01-03T00:05:00Z",
    finished_at: "2026-01-03T00:05:02Z",
    sources: [],
    steps: [],
    artifacts: [],
    ...overrides,
  };
}

export function page<T>(items: T[], pageSize = 20) {
  return { items, page: 1, page_size: pageSize, total: items.length };
}

export const EMPTY_PAGE = page([]);
