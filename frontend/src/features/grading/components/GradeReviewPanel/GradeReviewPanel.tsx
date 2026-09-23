/**
 * 教师复核与发布（契约 9.8 / 9.9）。
 *
 * 两条硬规则直接决定这里的交互：
 *
 * 1. **复核请求是完整快照**：`items` 必须恰好覆盖该提交引用版本的全部评分项，
 *    缺失、多余、重复或跨版本都会被后端拒绝（`422 VALIDATION_ERROR`）。
 *    因此界面渲染**全部**评分项、提交时也发全部，而不是只发被改过的那几项。
 * 2. **发布要求已复核**：未复核时后端返回 `409 GRADE_NOT_REVIEWED`，
 *    所以未复核前「发布成绩」按钮是禁用状态，并说明原因。
 *
 * 总分不在前端定稿：服务端按分项求和（`Decimal`）。这里展示的合计只是
 * 把当前输入加起来，便于教师在保存前自查。
 */

import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/Button/Button";
import { Card } from "@/components/Card/Card";
import { Pill } from "@/components/Pill/Pill";
import {
  usePublishGradeReview,
  useUpdateGradeReview,
} from "@/features/grading/hooks/useGradeReview";
import type { GradeItemVM, GradeReviewVM } from "@/features/grading/model/types";
import type { GradeReviewUpdateRequestDto } from "@/features/grading/api";
import { toAppError } from "@/services/http";
import { formatScore } from "@/utils/format";

import { GradeItemCard, type GradeItemDraft } from "../GradeItemCard/GradeItemCard";

import styles from "./GradeReviewPanel.module.css";

export type GradeReviewPanelProps = {
  review: GradeReviewVM;
  submissionId: string;
  assignmentId: string;
  /** 已发布后只读 */
  readOnly?: boolean;
};

type DraftMap = Record<string, GradeItemDraft>;

function seedDrafts(items: GradeItemVM[]): DraftMap {
  const drafts: DraftMap = {};
  for (const item of items) {
    drafts[item.id] = {
      finalScore: formatScore(item.finalScore),
      teacherComment: item.teacherComment,
    };
  }
  return drafts;
}

/** 分数只允许最多两位小数，且不超过该项满分 */
function validateItem(draft: GradeItemDraft, maxScore: number): string | null {
  const value = Number(draft.finalScore);
  if (draft.finalScore.trim() === "" || !Number.isFinite(value)) {
    return "请填写分数（只接受数字）。";
  }
  if (value < 0) return "分数不能为负。";
  if (value > maxScore) return `不能超过该项满分 ${formatScore(maxScore)}。`;
  if (Math.round(value * 100) !== Number((value * 100).toFixed(6))) {
    return "最多保留两位小数。";
  }
  return null;
}

export function GradeReviewPanel({
  review,
  submissionId,
  assignmentId,
  readOnly = false,
}: GradeReviewPanelProps) {
  const [drafts, setDrafts] = useState<DraftMap>(() => seedDrafts(review.items));
  const [summary, setSummary] = useState(review.teacherSummary || review.aiSummary);
  const [touched, setTouched] = useState(false);

  const updateReview = useUpdateGradeReview(submissionId, assignmentId);
  const publishReview = usePublishGradeReview(submissionId, assignmentId);

  // 复核保存成功或首次拿到数据时，用服务端结果重置草稿
  useEffect(() => {
    setDrafts(seedDrafts(review.items));
    setSummary(review.teacherSummary || review.aiSummary);
    setTouched(false);
  }, [review]);

  const problems = useMemo(() => {
    const result: Record<string, string | null> = {};
    for (const item of review.items) {
      const draft = drafts[item.id];
      result[item.id] = draft ? validateItem(draft, item.maxScore) : null;
    }
    return result;
  }, [drafts, review.items]);

  const hasProblem = Object.values(problems).some((value) => Boolean(value));
  const localTotal = useMemo(() => {
    let total = 0;
    for (const item of review.items) {
      const draft = drafts[item.id];
      const value = Number(draft?.finalScore ?? item.finalScore);
      total += Number.isFinite(value) ? value : 0;
    }
    // 分项求和会有浮点噪声，展示前按分位取整（服务端用 Decimal 计算）
    return Math.round(total * 100) / 100;
  }, [drafts, review.items]);

  const actionError =
    updateReview.isError && updateReview.error
      ? toAppError(updateReview.error)
      : publishReview.isError && publishReview.error
        ? toAppError(publishReview.error)
        : null;

  function save() {
    setTouched(true);
    if (hasProblem) return;

    // 完整快照：把界面上全部评分项一起提交
    const body: GradeReviewUpdateRequestDto = {
      summary: summary.trim(),
      items: review.items.map((item) => ({
        rubric_item_id: item.rubricItemId,
        final_score: Number(drafts[item.id].finalScore),
        teacher_comment: drafts[item.id].teacherComment.trim(),
      })),
    };
    updateReview.mutate({ reviewId: review.id, body });
  }

  const summaryTooLong = summary.trim().length > 20_000;
  const summaryEmpty = summary.trim().length === 0;

  return (
    <Card>
      <div className={styles.head}>
        <div>
          <h2 className={styles.title}>批改与复核</h2>
          <p className={styles.subtitle}>
            AI 建议合计 <strong>{formatScore(review.suggestedTotalScore)}</strong> 分 ·
            当前输入合计 <strong>{formatScore(localTotal)}</strong> / {formatScore(review.maxTotal)} 分
          </p>
        </div>
        <div className={styles.pills}>
          {review.isPublished ? (
            <Pill tone="success">已发布</Pill>
          ) : review.isReviewed ? (
            <Pill tone="info">已复核，待发布</Pill>
          ) : (
            <Pill tone="warn">未复核</Pill>
          )}
        </div>
      </div>

      {review.aiSummary ? (
        <div className={styles.aiSummary}>
          <span className={styles.label}>AI 总结</span>
          <p>{review.aiSummary}</p>
        </div>
      ) : null}

      {review.isPublished ? (
        <p className={styles.locked}>
          成绩已于 {review.publishedAtLabel} 发布，不能再修改。
        </p>
      ) : null}

      {actionError ? (
        <p className={styles.error} role="alert">
          {actionError.message}
        </p>
      ) : null}

      <div className={styles.items}>
        {review.items.map((item) => (
          <GradeItemCard
            key={item.id}
            item={item}
            mode="teacher"
            draft={drafts[item.id]}
            problem={touched ? problems[item.id] : null}
            onDraftChange={(patch) =>
              setDrafts((current) => ({
                ...current,
                [item.id]: { ...current[item.id], ...patch },
              }))
            }
          />
        ))}
      </div>

      <div className={styles.summaryField}>
        <label className={styles.label} htmlFor="review-summary">
          复核总结（学生可见，必填）
        </label>
        <p className={styles.hint}>
          起始内容取自 AI 建议，请确认或改写后再保存。
        </p>
        <textarea
          id="review-summary"
          className={styles.summaryInput}
          rows={4}
          maxLength={20_000}
          readOnly={readOnly || review.isPublished}
          value={summary}
          onChange={(event) => setSummary(event.target.value)}
        />
        {summaryEmpty ? (
          <p className={styles.problem}>总结不能为空。</p>
        ) : null}
        {summaryTooLong ? (
          <p className={styles.problem}>总结不能超过 20000 个字符。</p>
        ) : null}
      </div>

      {!review.isPublished && !readOnly ? (
        <div className={styles.actions}>
          <Button
            variant="primary"
            size="sm"
            disabled={updateReview.isPending || summaryEmpty || summaryTooLong}
            onClick={save}
          >
            {updateReview.isPending ? "保存中…" : "保存复核"}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={publishReview.isPending || !review.isReviewed}
            title={review.isReviewed ? "" : "需要先完成复核才能发布"}
            onClick={() => publishReview.mutate(review.id)}
          >
            {publishReview.isPending ? "发布中…" : "发布成绩"}
          </Button>
          <span className={styles.actionHint}>
            {review.isReviewed
              ? "发布后学生即可看到成绩与评语，且不可再修改。"
              : "保存复核后才能发布；发布后不可再修改。"}
          </span>
        </div>
      ) : null}
    </Card>
  );
}
