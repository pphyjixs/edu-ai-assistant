/**
 * 单个评分项的批改结果卡片。
 *
 * 教师与学生共用同一个组件，但**可见字段不同**（契约 9.7）：
 *
 * - 教师视角：AI 建议分、AI 判断说明、教师终稿与修改差值；
 * - 学生视角：只有终稿分数、教师评语，以及证据（原文 + 定位）与改进建议。
 *   AI 原始建议分对学生是 `null`，这里显式判断而不是把 null 当 0 分显示。
 *
 * 证据定位的单位由服务端按报告 MIME 决定（PDF 页码 / DOCX 段落号），
 * 前端只格式化，不猜测。
 */

import { Icon } from "@/components/Icon/Icon";
import { Pill } from "@/components/Pill/Pill";
import {
  evidenceSourceLabel,
  type GradeItemVM,
} from "@/features/grading/model/types";
import { formatScore } from "@/utils/format";

import styles from "./GradeItemCard.module.css";

export type GradeItemDraft = {
  finalScore: string;
  teacherComment: string;
};

export type GradeItemCardProps = {
  item: GradeItemVM;
  mode: "teacher" | "student";
  /** 教师视角下的草稿值（受控） */
  draft?: GradeItemDraft;
  onDraftChange?: (patch: Partial<GradeItemDraft>) => void;
  /** 本地校验出的问题（例如超过满分），提交前就提示而不是等 422 */
  problem?: string | null;
};

export function GradeItemCard({
  item,
  mode,
  draft,
  onDraftChange,
  problem,
}: GradeItemCardProps) {
  const isTeacher = mode === "teacher";
  const score = isTeacher ? (draft?.finalScore ?? formatScore(item.finalScore)) : formatScore(item.finalScore);

  return (
    <article className={styles.card}>
      <header className={styles.head}>
        <div className={styles.titleRow}>
          <span className={styles.order}>{String(item.order).padStart(2, "0")}</span>
          <h3 className={styles.title}>{item.title}</h3>
        </div>

        <div className={styles.scores}>
          {isTeacher && item.aiScore !== null ? (
            <Pill tone="neutral">AI 建议 {formatScore(item.aiScore)}</Pill>
          ) : null}
          {isTeacher && item.aiDelta !== null && item.aiDelta !== 0 ? (
            <Pill tone={item.aiDelta > 0 ? "success" : "warn"}>
              {item.aiDelta > 0 ? "上调" : "下调"} {formatScore(Math.abs(item.aiDelta))}
            </Pill>
          ) : null}
          <span className={styles.finalScore}>
            {isTeacher ? (
              <>
                <label className={styles.scoreLabel} htmlFor={`score-${item.id}`}>
                  终稿
                </label>
                <input
                  id={`score-${item.id}`}
                  className={problem ? `${styles.scoreInput} ${styles.scoreInvalid}` : styles.scoreInput}
                  type="number"
                  inputMode="decimal"
                  min={0}
                  max={item.maxScore}
                  step={0.5}
                  value={score}
                  onChange={(event) =>
                    onDraftChange?.({ finalScore: event.target.value })
                  }
                />
              </>
            ) : (
              <strong className={styles.scoreValue}>{score}</strong>
            )}
            <span className={styles.maxScore}>/ {formatScore(item.maxScore)}</span>
          </span>
        </div>
      </header>

      {problem ? (
        <p className={styles.problem} role="alert">
          {problem}
        </p>
      ) : null}

      {isTeacher && item.aiComment ? (
        <p className={styles.aiComment}>
          <span className={styles.label}>AI 判断</span>
          {item.aiComment}
        </p>
      ) : null}

      {item.errorType || item.improvementSuggestion ? (
        <div className={styles.findings}>
          {item.errorType ? (
            <p className={styles.finding}>
              <span className={styles.label}>问题类型</span>
              {item.errorType}
            </p>
          ) : null}
          {item.improvementSuggestion ? (
            <p className={styles.finding}>
              <span className={styles.label}>改进建议</span>
              {item.improvementSuggestion}
            </p>
          ) : null}
        </div>
      ) : null}

      {item.evidenceQuote ? (
        <blockquote className={styles.evidence}>
          <span className={styles.evidenceHead}>
            <Icon name="file" size={12} />
            <span>
              报告依据
              {item.evidenceLocationLabel
                ? ` · ${evidenceSourceLabel(item.evidenceSourceType)} ${item.evidenceLocationLabel}`
                : ""}
            </span>
          </span>
          <span className={styles.evidenceText}>{item.evidenceQuote}</span>
        </blockquote>
      ) : null}

      {isTeacher ? (
        <div className={styles.commentField}>
          <label className={styles.label} htmlFor={`comment-${item.id}`}>
            教师评语
          </label>
          <textarea
            id={`comment-${item.id}`}
            className={styles.commentInput}
            rows={2}
            maxLength={2000}
            placeholder="写给学生看的说明（可留空）"
            value={draft?.teacherComment ?? item.teacherComment}
            onChange={(event) =>
              onDraftChange?.({ teacherComment: event.target.value })
            }
          />
        </div>
      ) : item.teacherComment ? (
        <p className={styles.teacherComment}>
          <span className={styles.label}>教师评语</span>
          {item.teacherComment}
        </p>
      ) : null}
    </article>
  );
}
