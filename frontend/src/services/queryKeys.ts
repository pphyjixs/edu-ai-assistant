/**
 * TanStack Query 键集中定义（见 DEVELOPMENT_SPEC.md 第 27 节）。
 * 集中在一处的目的是让 invalidate 时不会因为手写字符串造成遗漏。
 *
 * 键的划分对齐契约里的资源层级：课程 → 课程下的集合（资料 / 练习 / 会话），
 * 以及按 ID 直接访问的单体资源。
 */

export const queryKeys = {
  /* ------------------------------ 用户 ------------------------------ */
  me: ["me"] as const,

  /* ---------------------------- Dashboard ---------------------------- */
  dashboardTeacher: ["dashboard", "teacher"] as const,
  dashboardStudent: ["dashboard", "student"] as const,

  /* ------------------------------ 课程 ------------------------------ */
  courses: ["courses"] as const,
  course: (courseId: string) => ["course", courseId] as const,
  courseMembers: (courseId: string) => ["course-members", courseId] as const,

  /* ------------------------------ 资料 ------------------------------ */
  materials: (courseId: string) => ["materials", courseId] as const,
  material: (materialId: string) => ["material", materialId] as const,
  materialOutline: (materialId: string) => ["material-outline", materialId] as const,

  /* ------------------------------ 练习 ------------------------------ */
  practiceSets: (courseId: string) => ["practice-sets", courseId] as const,
  practiceSet: (setId: string) => ["practice-set", setId] as const,
  practiceAttempt: (attemptId: string) => ["practice-attempt", attemptId] as const,

  /* ------------------------------ 会话 ------------------------------ */
  chatSessions: (courseId: string) => ["chat-sessions", courseId] as const,
  messages: (sessionId: string) => ["messages", sessionId] as const,

  /* ---------------------- 尚未实现（保留 mock）---------------------- */
  assignments: (courseId: string) => ["assignments", courseId] as const,
  assignment: (assignmentId: string) => ["assignment", assignmentId] as const,
  submission: (assignmentId: string, userId: string) =>
    ["submission", assignmentId, userId] as const,
  grade: (assignmentId: string, userId: string) => ["grade", assignmentId, userId] as const,

  /* ---------------------------- 异步任务 ---------------------------- */
  job: (jobId: string) => ["job", jobId] as const,
  /** 异步 Agent Run（Buddy 提问） */
  agentRun: (runId: string) => ["agent-run", runId] as const,
};
