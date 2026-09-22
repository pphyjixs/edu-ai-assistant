/**
 * 练习的视图模型。
 *
 * 契约 7.4 的答案可见性是这里的关键：学生响应里
 * `correct_answer` / `grading_points` / `explanation` 为 null，
 * VM 里同样保留为 null，界面据此决定是否渲染答案区。
 */

import type { PillTone } from "@/components/Pill/Pill";
import { formatMonthDayTime } from "@/utils/datetime";

import type {
  PracticeAttemptAnswerDto,
  PracticeAttemptResultDto,
  PracticeDifficultyDto,
  PracticeOptionDto,
  PracticeQuestionDto,
  PracticeQuestionTypeDto,
  PracticeSetDto,
  PracticeSetSummaryDto,
  PracticeStatusDto,
} from "../api";

/* ------------------------------ 枚举映射 ------------------------------ */

export type PracticeStatus = "generating" | "draft" | "published" | "failed" | "cancelled";

const STATUS_MAP: Record<PracticeStatusDto, { label: string; tone: PillTone; status: PracticeStatus }> = {
  GENERATING: { label: "生成中", tone: "info", status: "generating" },
  DRAFT: { label: "草稿", tone: "neutral", status: "draft" },
  PUBLISHED: { label: "已发布", tone: "success", status: "published" },
  FAILED: { label: "生成失败", tone: "danger", status: "failed" },
  CANCELLED: { label: "已取消", tone: "neutral", status: "cancelled" },
};

const DIFFICULTY_LABEL: Record<PracticeDifficultyDto, string> = {
  EASY: "简单",
  MEDIUM: "中等",
  HARD: "困难",
};

const QUESTION_TYPE_LABEL: Record<PracticeQuestionTypeDto, string> = {
  SINGLE_CHOICE: "单选题",
  TRUE_FALSE: "判断题",
  SHORT_ANSWER: "简答题",
};

export function questionTypeLabel(type: PracticeQuestionTypeDto): string {
  return QUESTION_TYPE_LABEL[type];
}

/* ------------------------------ 摘要 ------------------------------ */

export type PracticeSetSummaryVM = {
  id: string;
  courseId: string;
  title: string;
  status: PracticeStatus;
  statusLabel: string;
  statusTone: PillTone;
  difficultyLabel: string;
  questionCount: number;
  typeLabels: string[];
  createdAtLabel: string;
  publishedAtLabel: string | null;
};

export function toPracticeSetSummaryVM(dto: PracticeSetSummaryDto): PracticeSetSummaryVM {
  const mapped = STATUS_MAP[dto.status];
  return {
    id: dto.id,
    courseId: dto.course_id,
    title: dto.title,
    status: mapped.status,
    statusLabel: mapped.label,
    statusTone: mapped.tone,
    difficultyLabel: DIFFICULTY_LABEL[dto.difficulty],
    questionCount: dto.question_count,
    typeLabels: dto.question_types.map(questionTypeLabel),
    createdAtLabel: formatMonthDayTime(dto.created_at),
    publishedAtLabel: dto.published_at ? formatMonthDayTime(dto.published_at) : null,
  };
}

/* ------------------------------ 详情 ------------------------------ */

const OPTION_LETTERS = "ABCDEF";

export type PracticeOptionVM = {
  id: string;
  /** 选项序号（A/B/C…），仅用于展示 */
  letter: string;
  text: string;
};

export type PracticeGradingPointVM = {
  point: string;
  accepted: string[];
};

export type PracticeQuestionVM = {
  id: string;
  order: number;
  type: PracticeQuestionTypeDto;
  typeLabel: string;
  prompt: string;
  options: PracticeOptionVM[];
  knowledgePoint: string | null;
  /** 以下三项仅教师可见；学生响应里为 null / 空数组 */
  correctAnswer: string | boolean | null;
  gradingPoints: PracticeGradingPointVM[];
  explanation: string | null;
};

export type PracticeSetVM = PracticeSetSummaryVM & {
  questions: PracticeQuestionVM[];
  /** 是否可以看到答案（教师视角，或已提交后的结果页另行处理） */
  answersVisible: boolean;
};

function toOptionVM(dto: PracticeOptionDto, index: number): PracticeOptionVM {
  return {
    id: dto.id,
    letter: OPTION_LETTERS[index] ?? String(index + 1),
    text: dto.text,
  };
}

function toQuestionVM(dto: PracticeQuestionDto): PracticeQuestionVM {
  return {
    id: dto.id,
    order: dto.order,
    type: dto.type,
    typeLabel: QUESTION_TYPE_LABEL[dto.type],
    prompt: dto.prompt,
    options: dto.options.map(toOptionVM),
    knowledgePoint: dto.knowledge_point,
    correctAnswer: dto.correct_answer ?? null,
    gradingPoints: dto.grading_points ?? [],
    explanation: dto.explanation ?? null,
  };
}

export function toPracticeSetVM(dto: PracticeSetDto): PracticeSetVM {
  const questions = [...dto.questions]
    .sort((a, b) => a.order - b.order)
    .map(toQuestionVM);

  return {
    ...toPracticeSetSummaryVM(dto),
    questions,
    answersVisible: questions.some((question) => question.correctAnswer !== null),
  };
}

/* ------------------------------ 答题结果 ------------------------------ */

export type AttemptAnswerVM = {
  questionId: string;
  order: number;
  typeLabel: string;
  prompt: string;
  /** 展示用的提交答案：单选转成选项文本，判断题转成「正确 / 错误」 */
  submittedLabel: string;
  correctLabel: string;
  isCorrect: boolean;
  score: number;
  explanation: string;
  knowledgePoint: string | null;
};

export type PracticeAttemptVM = {
  id: string;
  practiceSetId: string;
  totalScore: number;
  submittedAtLabel: string;
  answers: AttemptAnswerVM[];
  correctCount: number;
  questionCount: number;
};

function optionTextOf(questionId: string, value: unknown, sets: PracticeSetVM | undefined): string {
  if (typeof value === "boolean") return value ? "正确" : "错误";
  if (typeof value !== "string") return "—";

  const question = sets?.questions.find((item) => item.id === questionId);
  const option = question?.options.find((item) => item.id === value);
  // 单选答案存的是选项 ID；找不到对应选项时原样展示，不猜内容
  return option ? `${option.letter}. ${option.text}` : value;
}

function toAttemptAnswerVM(
  dto: PracticeAttemptAnswerDto,
  set: PracticeSetVM | undefined,
): AttemptAnswerVM {
  return {
    questionId: dto.question_id,
    order: dto.question_order,
    typeLabel: QUESTION_TYPE_LABEL[dto.type],
    prompt: dto.prompt,
    submittedLabel: optionTextOf(dto.question_id, dto.submitted_answer, set),
    correctLabel: optionTextOf(dto.question_id, dto.correct_answer, set),
    isCorrect: dto.is_correct,
    score: dto.score,
    explanation: dto.explanation,
    knowledgePoint: dto.knowledge_point,
  };
}

export function toPracticeAttemptVM(
  dto: PracticeAttemptResultDto,
  set: PracticeSetVM | undefined,
): PracticeAttemptVM {
  const answers = [...dto.answers]
    .sort((a, b) => a.question_order - b.question_order)
    .map((answer) => toAttemptAnswerVM(answer, set));

  return {
    id: dto.id,
    practiceSetId: dto.practice_set_id,
    totalScore: dto.total_score,
    submittedAtLabel: formatMonthDayTime(dto.submitted_at),
    answers,
    correctCount: answers.filter((answer) => answer.isCorrect).length,
    questionCount: answers.length,
  };
}
