/**
 * TanStack Query 键集中定义（见 DEVELOPMENT_SPEC.md 第 27 节）。
 * 集中在一处的目的是让 invalidate 时不会因为手写字符串造成遗漏。
 */

export const queryKeys = {
  me: ["me"] as const,

  dashboardTeacher: ["dashboard", "teacher"] as const,
  dashboardStudent: ["dashboard", "student"] as const,

  courses: ["courses"] as const,
  course: (courseId: string) => ["course", courseId] as const,
  courseMembers: (courseId: string) => ["course-members", courseId] as const,
  courseOutline: (courseId: string) => ["course-outline", courseId] as const,

  materials: (courseId: string) => ["materials", courseId] as const,
  material: (materialId: string) => ["material", materialId] as const,

  assignments: (courseId: string) => ["assignments", courseId] as const,
  assignment: (assignmentId: string) => ["assignment", assignmentId] as const,

  submission: (assignmentId: string, userId: string) =>
    ["submission", assignmentId, userId] as const,
  grade: (assignmentId: string, userId: string) => ["grade", assignmentId, userId] as const,

  chatSessions: (courseId: string) => ["chat-sessions", courseId] as const,
  messages: (sessionId: string) => ["messages", sessionId] as const,

  job: (jobId: string) => ["job", jobId] as const,
};
