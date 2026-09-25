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
  /** 原文下载地址：短时有效，因此不做长期缓存，过期后重新取 */
  materialDownloadUrl: (materialId: string) =>
    ["material-download-url", materialId] as const,

  /* ------------------------------ 练习 ------------------------------ */
  practiceSets: (courseId: string) => ["practice-sets", courseId] as const,
  practiceSet: (setId: string) => ["practice-set", setId] as const,
  practiceAttempt: (attemptId: string) => ["practice-attempt", attemptId] as const,

  /* ------------------------------ 会话 ------------------------------ */
  chatSessions: (courseId: string) => ["chat-sessions", courseId] as const,
  /** 会话详情：中央会话页刷新时据此恢复所属课程，不信 URL / sessionStorage */
  chatSession: (sessionId: string) => ["chat-session", sessionId] as const,
  messages: (sessionId: string) => ["messages", sessionId] as const,

  /* ------------------------------ 作业 ------------------------------ */
  assignments: (courseId: string) => ["assignments", courseId] as const,
  assignment: (assignmentId: string) => ["assignment", assignmentId] as const,
  /** 任务附件（契约 8.15）；列表里带短时下载地址，因此不做长期缓存 */
  assignmentAttachments: (assignmentId: string) =>
    ["assignment-attachments", assignmentId] as const,

  /* -------------------- 提交与批改（契约第 9 节） -------------------- */
  /** 某作业的提交列表：教师拿到全班，学生拿到本人 0–1 条 */
  submissions: (assignmentId: string) => ["submissions", assignmentId] as const,
  submission: (submissionId: string) => ["submission", submissionId] as const,
  /** 批改详情（AI 建议 + 教师终稿）；学生仅在发布后可读 */
  gradeReview: (submissionId: string) => ["grade-review", submissionId] as const,

  /* ---------------------------- 异步任务 ---------------------------- */
  job: (jobId: string) => ["job", jobId] as const,
  /** 异步 Agent Run（Buddy 提问） */
  agentRun: (runId: string) => ["agent-run", runId] as const,
  /** 会话内的 Run 列表：刷新后据此把失败原因显示在对应提问下面 */
  agentRuns: (sessionId: string) => ["agent-runs", sessionId] as const,
  /** 会话中尚未结束的 Run：刷新后据此恢复轮询 */
  activeAgentRun: (sessionId: string) => ["agent-active-run", sessionId] as const,
};
