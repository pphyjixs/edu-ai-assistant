/**
 * 创建 / 编辑实验任务（仅课程创建教师，契约 8.2 / 8.5）。
 *
 * 本地校验与契约逐条对齐，目的是让教师在**提交前**就看到问题，
 * 而不是拿到一个 422：
 * - `title` 去空白后 1–200；`description` ≤ 20000；
 * - `total_score` 正数、最多两位小数；
 * - 评分项 1–50 项，每项 `max_score` 正数、最多两位小数，顺序即 `order`；
 * - **各评分项分值之和必须精确等于总分**，否则后端返回 `RUBRIC_SCORE_MISMATCH`。
 */

import { useState } from "react";

import { Button } from "@/components/Button/Button";
import {
  rubricDraftSum,
  toRubricDraft,
  toRubricRequest,
  type AssignmentVM,
  type RubricDraftItem,
} from "@/features/assignments/model/types";
import type {
  AssignmentCreateRequestDto,
  AssignmentUpdateRequestDto,
} from "@/features/assignments/api";
import { toAppError } from "@/services/http";
import { RUBRIC_SUGGEST_MAX_MB } from "@/services/upload";

import styles from "./AssignmentForm.module.css";

const TITLE_MAX = 200;
const DESCRIPTION_MAX = 20_000;
const MAX_RUBRIC_ITEMS = 50;

const MONEY_PATTERN = /^\d+(\.\d{1,2})?$/;

let draftSeq = 0;
function newDraft(): RubricDraftItem {
  draftSeq += 1;
  return { key: `new-${draftSeq}`, title: "", description: "", maxScore: "" };
}

/** datetime-local 需要 `YYYY-MM-DDTHH:mm`，把后端 UTC 时间转成本地输入值 */
function toLocalInputValue(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

export type AssignmentFormProps = {
  /** 传入表示编辑已有任务 */
  assignment?: AssignmentVM;
  isPending: boolean;
  error: unknown;
  onSubmitCreate: (body: AssignmentCreateRequestDto, attachmentFile?: File) => void;
  onSuggestRubric?: (file: File, description: string, totalScore: number) => Promise<AssignmentCreateRequestDto["rubric_items"]>;
  /** 仅编辑场景需要 */
  onSubmitUpdate?: (body: AssignmentUpdateRequestDto) => void;
  onCancel?: () => void;
};

export function AssignmentForm({
  assignment,
  isPending,
  error,
  onSubmitCreate,
  onSuggestRubric,
  onSubmitUpdate,
  onCancel,
}: AssignmentFormProps) {
  const [title, setTitle] = useState(assignment?.title ?? "");
  const [description, setDescription] = useState(assignment?.description ?? "");
  const [totalScore, setTotalScore] = useState(
    assignment ? String(assignment.totalScore) : "100",
  );
  const [dueAt, setDueAt] = useState(toLocalInputValue(assignment?.dueAt ?? null));
  const [allowLate, setAllowLate] = useState(assignment?.allowLateSubmission ?? false);
  const [rubric, setRubric] = useState<RubricDraftItem[]>(
    assignment && assignment.rubric.length > 0
      ? toRubricDraft(assignment.rubric)
      : [newDraft()],
  );
  const [problems, setProblems] = useState<string[]>([]);
  const [automaticRubric, setAutomaticRubric] = useState(!assignment);
  const [attachmentFile, setAttachmentFile] = useState<File | null>(null);
  const [rubricReady, setRubricReady] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [suggestionError, setSuggestionError] = useState("");

  const sum = rubricDraftSum(rubric);
  const total = Number(totalScore);
  const mismatch = Number.isFinite(total) && Math.abs(sum - total) > 1e-9;
  const appError = error ? toAppError(error) : null;

  function updateRubricItem(key: string, patch: Partial<RubricDraftItem>) {
    setRubric((items) => items.map((item) => (item.key === key ? { ...item, ...patch } : item)));
  }

  function validate(): string[] {
    const found: string[] = [];

    if (title.trim().length === 0) found.push("请填写任务标题。");
    else if (title.trim().length > TITLE_MAX) found.push(`标题最多 ${TITLE_MAX} 个字符。`);

    if (description.length > DESCRIPTION_MAX) {
      found.push(`任务说明最多 ${DESCRIPTION_MAX} 个字符。`);
    }

    if (!MONEY_PATTERN.test(totalScore) || Number(totalScore) <= 0) {
      found.push("总分应为正数，且最多两位小数。");
    }

    if (automaticRubric && !rubricReady) {
      if (!attachmentFile) found.push("自动解析评分项需要先选择作业文件。");
      return found;
    }
    if (rubric.length === 0) found.push("至少需要一个评分项。");
    if (rubric.length > MAX_RUBRIC_ITEMS) found.push(`评分项最多 ${MAX_RUBRIC_ITEMS} 个。`);

    rubric.forEach((item, index) => {
      if (item.title.trim().length === 0) found.push(`第 ${index + 1} 个评分项还没有名称。`);
      if (!MONEY_PATTERN.test(item.maxScore) || Number(item.maxScore) <= 0) {
        found.push(`第 ${index + 1} 个评分项的分值应为正数，且最多两位小数。`);
      }
    });

    if (found.length === 0 && mismatch) {
      found.push(`评分项合计 ${sum} 分与总分 ${totalScore} 分不一致。`);
    }

    return found;
  }

  async function submit() {
    const found = validate();
    setProblems(found);
    if (found.length > 0) return;

    if (automaticRubric && !rubricReady && attachmentFile && onSuggestRubric) {
      setSuggesting(true);
      setSuggestionError("");
      try {
        const items = await onSuggestRubric(attachmentFile, description, Number(totalScore));
        setRubric(items.map((item, index) => ({
          key: `ai-${index}`, title: item.title, description: item.description ?? "",
          maxScore: String(item.max_score),
        })));
        setRubricReady(true);
        setProblems([]);
      } catch (cause) {
        setSuggestionError(cause instanceof Error ? cause.message : toAppError(cause).message);
      } finally {
        setSuggesting(false);
      }
      return;
    }

    const common = {
      title: title.trim(),
      description,
      total_score: Number(totalScore),
      // 空输入表示清除截止时间：契约里 `null` 与「省略」语义不同
      due_at: dueAt ? new Date(dueAt).toISOString() : null,
      allow_late_submission: allowLate,
      rubric_items: toRubricRequest(rubric),
    };

    if (assignment) {
      onSubmitUpdate?.(common);
    } else {
      onSubmitCreate(common, attachmentFile ?? undefined);
    }
  }

  return (
    <div className={styles.form}>
      <div className={styles.field}>
        <label className={styles.label} htmlFor="assignment-title">
          任务标题
        </label>
        <input
          id="assignment-title"
          className={styles.input}
          value={title}
          maxLength={TITLE_MAX}
          placeholder="例如：实验二 Transformer 文本分类"
          onChange={(event) => setTitle(event.target.value)}
        />
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor="assignment-description">
          任务说明
        </label>
        <textarea
          id="assignment-description"
          className={styles.textarea}
          rows={3}
          maxLength={DESCRIPTION_MAX}
          value={description}
          placeholder="学生需要做什么、提交什么"
          onChange={(event) => { setDescription(event.target.value); setRubricReady(false); }}
        />
      </div>

      <div className={styles.row}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor="assignment-total">
            总分
          </label>
          <input
            id="assignment-total"
            className={styles.input}
            inputMode="decimal"
            value={totalScore}
            onChange={(event) => { setTotalScore(event.target.value); setRubricReady(false); }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor="assignment-due">
            截止时间
          </label>
          <input
            id="assignment-due"
            className={styles.input}
            type="datetime-local"
            value={dueAt}
            onChange={(event) => setDueAt(event.target.value)}
          />
          <p className={styles.hint}>留空表示不设截止时间；清空已有值会清除截止时间。</p>
        </div>
      </div>

      <label className={styles.checkbox}>
        <input
          type="checkbox"
          checked={allowLate}
          onChange={(event) => setAllowLate(event.target.checked)}
        />
        允许补交（手工关闭仍然优先于补交设置）
      </label>

      {!assignment ? (
        <div className={styles.field}>
          <label className={styles.label} htmlFor="assignment-create-file">作业文件</label>
          <input id="assignment-create-file" type="file" accept=".pdf,.pptx,.docx"
            onChange={(event) => {
              setAttachmentFile(event.target.files?.[0] ?? null);
              setRubricReady(false);
            }} />
          <p className={styles.hint}>
            创建时上传为作业附件；自动解析评分项需要文件不超过 {RUBRIC_SUGGEST_MAX_MB} MB。
          </p>
          {attachmentFile ? <p className={styles.hint}>已选择：{attachmentFile.name}</p> : null}
        </div>
      ) : null}

      <div className={styles.rubricBlock}>
        <div className={styles.rubricHead}>
          <span className={styles.label}>评分标准</span>
          <span className={mismatch && (!automaticRubric || rubricReady) ? styles.sumBad : styles.sum}>
            合计 {sum} / 总分 {Number.isFinite(total) ? totalScore : "—"}
          </span>
        </div>

        {!assignment ? <label className={styles.checkbox}>
          <input type="checkbox" checked={automaticRubric}
            onChange={(event) => { setAutomaticRubric(event.target.checked); setProblems([]); }} />
          AI 自动解析评分项
        </label> : null}
        {automaticRubric && !rubricReady ? (
          <p className={styles.hint}>选择作业文件后点击“AI 解析评分项”；查看建议并可修改，再创建任务。</p>
        ) : null}

        {(!automaticRubric || rubricReady) && <ul className={styles.rubricList}>
          {rubric.map((item, index) => (
            <li className={styles.rubricItem} key={item.key}>
              <span className={styles.rubricOrder}>{index + 1}</span>
              <div className={styles.rubricFields}>
                <input
                  className={styles.input}
                  value={item.title}
                  placeholder="评分项名称"
                  aria-label={`第 ${index + 1} 个评分项名称`}
                  onChange={(event) => updateRubricItem(item.key, { title: event.target.value })}
                />
                <input
                  className={styles.input}
                  value={item.description}
                  placeholder="说明（可选）"
                  aria-label={`第 ${index + 1} 个评分项说明`}
                  onChange={(event) =>
                    updateRubricItem(item.key, { description: event.target.value })
                  }
                />
              </div>
              <input
                className={styles.scoreInput}
                inputMode="decimal"
                value={item.maxScore}
                placeholder="分值"
                aria-label={`第 ${index + 1} 个评分项分值`}
                onChange={(event) => updateRubricItem(item.key, { maxScore: event.target.value })}
              />
              <Button
                variant="ghost"
                size="sm"
                aria-label={`删除第 ${index + 1} 个评分项`}
                disabled={rubric.length <= 1}
                onClick={() => setRubric((items) => items.filter((row) => row.key !== item.key))}
              >
                删除
              </Button>
            </li>
          ))}
        </ul>}

        {(!automaticRubric || rubricReady) && <Button
          variant="secondary"
          size="sm"
          disabled={rubric.length >= MAX_RUBRIC_ITEMS}
          onClick={() => setRubric((items) => [...items, newDraft()])}
        >
          添加评分项
        </Button>}
      </div>

      {suggestionError ? <p className={styles.error} role="alert">{suggestionError}</p> : null}

      {problems.length > 0 ? (
        <ul className={styles.problems} role="alert">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      ) : null}

      {appError ? (
        <p className={styles.error} role="alert">
          {appError.message}
        </p>
      ) : null}

      <div className={styles.footer}>
        <Button variant="primary" onClick={submit} disabled={isPending || suggesting}>
          {suggesting ? "AI 解析中…" : isPending ? "保存中…" : assignment ? "保存修改" : automaticRubric && !rubricReady ? "AI 解析评分项" : "创建任务"}
        </Button>
        {onCancel ? (
          <Button variant="ghost" onClick={onCancel}>
            取消
          </Button>
        ) : null}
      </div>
    </div>
  );
}
