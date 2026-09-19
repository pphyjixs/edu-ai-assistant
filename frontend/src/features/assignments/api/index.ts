/**
 * Assignments 的 API 入口。
 *
 * 后端实现后换成基于 services/http 的实现：
 *   http.get<AssignmentDto[]>(`/courses/${courseId}/assignments`)
 *   http.get<AssignmentDto>(`/assignments/${assignmentId}`)
 */

import type { AssignmentsApi } from "./contracts";
import { mockAssignmentsApi } from "./mock";

export const assignmentsApi: AssignmentsApi = mockAssignmentsApi;

export type {
  AssignmentDto,
  AssignmentStatusDto,
  AssignmentsApi,
  CreateSubmissionBody,
  RubricItemDto,
  SubmissionDto,
  SubmissionStatusDto,
} from "./contracts";
