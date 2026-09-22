/**
 * 答题结果。
 *
 * 学生只能通过这里看到标准答案与解析（契约 7.7）——
 * 练习详情里不含这些字段，因此这个视图本身就是「教师发布之后」的那一步。
 * 得分与明细全部来自后端评分（7.9），前端不做任何再计算。
 */

import { Card } from "@/components/Card/Card";
import { Pill } from "@/components/Pill/Pill";
import type { PracticeAttemptVM } from "@/features/practice/model/types";

import styles from "./PracticeResultView.module.css";

export type PracticeResultViewProps = {
  attempt: PracticeAttemptVM;
  /** 需要教师视角标注「这是某位学生的作答」时传入 */
  studentNote?: string;
};

export function PracticeResultView({ attempt, studentNote }: PracticeResultViewProps) {
  return (
    <div className={styles.wrapper}>
      <Card className={styles.summary}>
        <div className={styles.scoreBlock}>
          <span className={styles.scoreLabel}>总分</span>
          <span className={styles.scoreValue}>{attempt.totalScore.toFixed(2)}</span>
          <span className={styles.scoreUnit}>/ 100</span>
        </div>
        <div className={styles.summaryMeta}>
          <p className={styles.summaryLine}>
            答对 {attempt.correctCount} / {attempt.questionCount} 题
          </p>
          <p className={styles.summaryLine}>提交于 {attempt.submittedAtLabel}</p>
          {studentNote ? <p className={styles.summaryLine}>{studentNote}</p> : null}
        </div>
      </Card>

      <ol className={styles.answers}>
        {attempt.answers.map((answer) => (
          <li className={styles.answer} key={answer.questionId}>
            <div className={styles.answerHead}>
              <span className={styles.order}>{String(answer.order).padStart(2, "0")}</span>
              <span className={styles.prompt}>{answer.prompt}</span>
              <span className={styles.verdict}>
                <Pill tone={answer.isCorrect ? "success" : "danger"}>
                  {answer.isCorrect ? "正确" : "错误"}
                </Pill>
                <span className={styles.score}>{answer.score.toFixed(2)} 分</span>
              </span>
            </div>

            <dl className={styles.compare}>
              <div className={styles.compareRow}>
                <dt>你的作答</dt>
                <dd className={answer.isCorrect ? styles.ok : styles.wrong}>
                  {answer.submittedLabel}
                </dd>
              </div>
              <div className={styles.compareRow}>
                <dt>标准答案</dt>
                <dd>{answer.correctLabel}</dd>
              </div>
            </dl>

            {answer.explanation ? (
              <div className={styles.explanation}>
                <span className={styles.explanationLabel}>解析</span>
                <p>{answer.explanation}</p>
              </div>
            ) : null}

            <div className={styles.tags}>
              <span className={styles.type}>{answer.typeLabel}</span>
              {answer.knowledgePoint ? (
                <span className={styles.knowledgePoint}>{answer.knowledgePoint}</span>
              ) : null}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
