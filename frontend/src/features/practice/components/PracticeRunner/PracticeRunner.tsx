/**
 * 答题区。
 *
 * 契约 7.6 的校验很严格：提交必须**恰好覆盖全部题目**，
 * 单选答案必须是选项 ID（字符串）、判断答案必须是 JSON 布尔值、
 * 简答去空白后非空。因此这里在提交前先做一遍等价的本地校验，
 * 避免让用户用一个 422 才发现漏做了一题。
 */

import { useState } from "react";

import { Button } from "@/components/Button/Button";
import type { PracticeSetVM } from "@/features/practice/model/types";
import { useSubmitAttempt } from "@/features/practice/hooks/usePractice";
import { toAppError } from "@/services/http";

import styles from "./PracticeRunner.module.css";

const SHORT_ANSWER_MAX = 2000;

type AnswerValue = string | boolean;

export type PracticeRunnerProps = {
  courseId: string;
  practiceSet: PracticeSetVM;
  onSubmitted: (attemptId: string) => void;
};

export function PracticeRunner({ courseId, practiceSet, onSubmitted }: PracticeRunnerProps) {
  const [answers, setAnswers] = useState<Record<string, AnswerValue>>({});
  const [problems, setProblems] = useState<string[]>([]);
  const submit = useSubmitAttempt(courseId, practiceSet.id);

  const error = submit.isError ? toAppError(submit.error) : null;

  function setAnswer(questionId: string, value: AnswerValue) {
    setAnswers((current) => ({ ...current, [questionId]: value }));
  }

  function validate(): string[] {
    const found: string[] = [];

    for (const question of practiceSet.questions) {
      const value = answers[question.id];

      if (value === undefined) {
        found.push(`第 ${question.order} 题还没有作答。`);
        continue;
      }
      if (question.type === "SHORT_ANSWER" && typeof value === "string" && value.trim().length === 0) {
        found.push(`第 ${question.order} 题的简答内容为空。`);
      }
    }

    return found;
  }

  function handleSubmit() {
    const found = validate();
    setProblems(found);
    if (found.length > 0) return;

    submit.mutate(
      practiceSet.questions.map((question) => ({
        question_id: question.id,
        answer: answers[question.id] as AnswerValue,
      })),
      { onSuccess: (result) => onSubmitted(result.id) },
    );
  }

  const answeredCount = practiceSet.questions.filter(
    (question) => answers[question.id] !== undefined,
  ).length;

  return (
    <div className={styles.wrapper}>
      <ol className={styles.questions}>
        {practiceSet.questions.map((question) => (
          <li className={styles.question} key={question.id}>
            <div className={styles.questionHead}>
              <span className={styles.order}>{String(question.order).padStart(2, "0")}</span>
              <span className={styles.prompt}>{question.prompt}</span>
            </div>
            <div className={styles.tags}>
              <span className={styles.type}>{question.typeLabel}</span>
              {question.knowledgePoint ? (
                <span className={styles.knowledgePoint}>{question.knowledgePoint}</span>
              ) : null}
            </div>

            {question.type === "SHORT_ANSWER" ? (
              <div className={styles.field}>
                <label className="srOnly" htmlFor={`answer-${question.id}`}>
                  第 {question.order} 题的作答
                </label>
                <textarea
                  id={`answer-${question.id}`}
                  className={styles.textarea}
                  rows={4}
                  maxLength={SHORT_ANSWER_MAX}
                  placeholder="写下你的作答…"
                  value={
                    typeof answers[question.id] === "string"
                      ? (answers[question.id] as string)
                      : ""
                  }
                  onChange={(event) => setAnswer(question.id, event.target.value)}
                />
              </div>
            ) : (
              <div className={styles.options}>
                {(question.type === "TRUE_FALSE"
                  ? [
                      { id: "true", letter: "", text: "正确" },
                      { id: "false", letter: "", text: "错误" },
                    ]
                  : question.options
                ).map((option) => {
                  const value: AnswerValue =
                    question.type === "TRUE_FALSE" ? option.id === "true" : option.id;
                  const checked = answers[question.id] === value;

                  return (
                    <label
                      key={option.id}
                      className={checked ? `${styles.option} ${styles.optionChecked}` : styles.option}
                    >
                      <input
                        type="radio"
                        name={`q-${question.id}`}
                        className="srOnly"
                        checked={checked}
                        onChange={() => setAnswer(question.id, value)}
                      />
                      {option.letter ? <span className={styles.letter}>{option.letter}</span> : null}
                      <span className={styles.optionText}>{option.text}</span>
                    </label>
                  );
                })}
              </div>
            )}
          </li>
        ))}
      </ol>

      {problems.length > 0 ? (
        <div className={styles.problems} role="alert">
          <p className={styles.problemsTitle}>还有 {problems.length} 处需要处理</p>
          <ul className={styles.problemsList}>
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {error ? (
        <p className={styles.error} role="alert">
          {error.message}
        </p>
      ) : null}

      <div className={styles.footer}>
        <span className={styles.progress}>
          已作答 {answeredCount} / {practiceSet.questions.length}
        </span>
        <Button variant="primary" onClick={handleSubmit} disabled={submit.isPending}>
          {submit.isPending ? "提交中…" : "提交答案"}
        </Button>
      </div>

      <p className={styles.note}>
        每套练习只能提交一次，提交后会立即看到得分与逐题解析。
      </p>
    </div>
  );
}
