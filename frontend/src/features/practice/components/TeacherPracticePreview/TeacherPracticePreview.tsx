/**
 * 教师视角的练习预览（含答案与解析）。
 *
 * 契约 7.4：教师响应包含 `correct_answer` / `grading_points` / `explanation`，
 * 学生响应里这些字段为 null。因此同一个页面按角色渲染出不同的信息量，
 * 而这个差别是由接口数据决定的，不是前端自己藏的。
 */

import { Button } from "@/components/Button/Button";
import { Pill } from "@/components/Pill/Pill";
import type { PracticeSetVM } from "@/features/practice/model/types";
import { usePublishPractice } from "@/features/practice/hooks/usePractice";
import { toAppError } from "@/services/http";

import styles from "./TeacherPracticePreview.module.css";

export type TeacherPracticePreviewProps = {
  courseId: string;
  practiceSet: PracticeSetVM;
  readOnly: boolean;
};

function answerLabel(value: string | boolean | null, options: { id: string; letter: string; text: string }[]): string {
  if (typeof value === "boolean") return value ? "正确" : "错误";
  if (typeof value !== "string") return "—";
  const option = options.find((item) => item.id === value);
  return option ? `${option.letter}. ${option.text}` : value;
}

export function TeacherPracticePreview({
  courseId,
  practiceSet,
  readOnly,
}: TeacherPracticePreviewProps) {
  const publish = usePublishPractice(courseId, practiceSet.id);
  const error = publish.isError ? toAppError(publish.error) : null;

  const canPublish = practiceSet.status === "draft" && !readOnly;

  return (
    <div className={styles.wrapper}>
      <div className={styles.toolbar}>
        <div className={styles.toolbarInfo}>
          <Pill tone={practiceSet.statusTone}>{practiceSet.statusLabel}</Pill>
          <span className={styles.meta}>
            {practiceSet.questionCount} 题 · {practiceSet.difficultyLabel}
          </span>
        </div>

        {canPublish ? (
          <Button
            variant="primary"
            size="sm"
            disabled={publish.isPending}
            onClick={() => publish.mutate()}
          >
            {publish.isPending ? "发布中…" : "发布给学生"}
          </Button>
        ) : null}

        {practiceSet.status === "generating" ? (
          <span className={styles.hint}>题目还在生成，完成后才能发布。</span>
        ) : null}
        {practiceSet.status === "failed" ? (
          <span className={styles.hint}>生成失败，可以在学习页重试。</span>
        ) : null}
        {practiceSet.status === "published" ? (
          <span className={styles.hint}>已发布，内容不可再修改。</span>
        ) : null}
      </div>

      {error ? (
        <p className={styles.error} role="alert">
          {error.message}
        </p>
      ) : null}

      <ol className={styles.questions}>
        {practiceSet.questions.map((question) => (
          <li className={styles.question} key={question.id}>
            <div className={styles.questionHead}>
              <span className={styles.order}>{String(question.order).padStart(2, "0")}</span>
              <span className={styles.prompt}>{question.prompt}</span>
              <span className={styles.type}>{question.typeLabel}</span>
            </div>

            {question.options.length > 0 ? (
              <ul className={styles.options}>
                {question.options.map((option) => (
                  <li key={option.id}>
                    <span className={styles.letter}>{option.letter}</span>
                    {option.text}
                  </li>
                ))}
              </ul>
            ) : null}

            <dl className={styles.answers}>
              <div>
                <dt>标准答案</dt>
                <dd>{answerLabel(question.correctAnswer, question.options)}</dd>
              </div>
              {question.gradingPoints.length > 0 ? (
                <div>
                  <dt>评分要点</dt>
                  <dd>
                    <ul className={styles.points}>
                      {question.gradingPoints.map((point) => (
                        <li key={point.point}>
                          <strong>{point.point}</strong>
                          {point.accepted.length > 0 ? `：${point.accepted.join(" / ")}` : ""}
                        </li>
                      ))}
                    </ul>
                  </dd>
                </div>
              ) : null}
              {question.explanation ? (
                <div>
                  <dt>解析</dt>
                  <dd>{question.explanation}</dd>
                </div>
              ) : null}
              {question.knowledgePoint ? (
                <div>
                  <dt>知识点</dt>
                  <dd>{question.knowledgePoint}</dd>
                </div>
              ) : null}
            </dl>
          </li>
        ))}
      </ol>
    </div>
  );
}
