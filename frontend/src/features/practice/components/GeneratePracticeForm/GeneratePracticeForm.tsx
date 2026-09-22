/**
 * 生成练习（仅课程创建教师，契约 7.2）。
 *
 * 本地校验与契约一致：资料 1–10 份不重复、题数 1–20 且不少于题型数量、
 * 题型非空不重复、难度取值合法。请求体里的 `question_count` 必须是
 * JSON 整数，因此这里显式转换成 number。
 */

import { useState } from "react";

import { Button } from "@/components/Button/Button";
import type { MaterialVM } from "@/features/materials/model/types";
import type { PracticeDifficultyDto, PracticeQuestionTypeDto } from "@/features/practice/api";
import { useGeneratePractice } from "@/features/practice/hooks/usePractice";
import { toAppError } from "@/services/http";

import styles from "./GeneratePracticeForm.module.css";

const TYPE_OPTIONS: Array<{ value: PracticeQuestionTypeDto; label: string }> = [
  { value: "SINGLE_CHOICE", label: "单选题" },
  { value: "TRUE_FALSE", label: "判断题" },
  { value: "SHORT_ANSWER", label: "简答题" },
];

const DIFFICULTY_OPTIONS: Array<{ value: PracticeDifficultyDto; label: string }> = [
  { value: "EASY", label: "简单" },
  { value: "MEDIUM", label: "中等" },
  { value: "HARD", label: "困难" },
];

const MAX_MATERIALS = 10;
const MAX_QUESTIONS = 20;

export type GeneratePracticeFormProps = {
  courseId: string;
  /** 只有解析完成的资料能被选中 */
  materials: MaterialVM[];
  onGenerated: (jobId: string, setId: string) => void;
};

export function GeneratePracticeForm({
  courseId,
  materials,
  onGenerated,
}: GeneratePracticeFormProps) {
  const [selectedMaterials, setSelectedMaterials] = useState<string[]>([]);
  const [questionCount, setQuestionCount] = useState(5);
  const [types, setTypes] = useState<PracticeQuestionTypeDto[]>(["SINGLE_CHOICE"]);
  const [difficulty, setDifficulty] = useState<PracticeDifficultyDto>("MEDIUM");
  const [problems, setProblems] = useState<string[]>([]);

  const generate = useGeneratePractice(courseId);
  const error = generate.isError ? toAppError(generate.error) : null;

  const readyMaterials = materials.filter((material) => material.isReady);

  function toggleMaterial(id: string) {
    setSelectedMaterials((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );
  }

  function toggleType(type: PracticeQuestionTypeDto) {
    setTypes((current) =>
      current.includes(type) ? current.filter((item) => item !== type) : [...current, type],
    );
  }

  function validate(): string[] {
    const found: string[] = [];

    if (selectedMaterials.length === 0) found.push("请选择至少一份已解析完成的资料。");
    if (selectedMaterials.length > MAX_MATERIALS) {
      found.push(`最多同时选择 ${MAX_MATERIALS} 份资料。`);
    }
    if (types.length === 0) found.push("请至少选择一种题型。");
    if (questionCount < 1 || questionCount > MAX_QUESTIONS) {
      found.push(`题数需要在 1–${MAX_QUESTIONS} 之间。`);
    }
    if (types.length > 0 && questionCount < types.length) {
      found.push("题数不能少于题型数量，每种题型至少要有一题。");
    }

    return found;
  }

  function handleSubmit() {
    const found = validate();
    setProblems(found);
    if (found.length > 0) return;

    generate.mutate(
      {
        material_ids: selectedMaterials,
        question_count: questionCount,
        question_types: types,
        difficulty,
      },
      {
        onSuccess: (job) => {
          onGenerated(job.id, job.resource_id);
          setSelectedMaterials([]);
        },
      },
    );
  }

  return (
    <div className={styles.form}>
      <div className={styles.field}>
        <span className={styles.label}>来源资料</span>
        {readyMaterials.length === 0 ? (
          <p className={styles.hint}>
            还没有解析完成的资料。等课件解析成功后才能生成练习。
          </p>
        ) : (
          <ul className={styles.materials}>
            {readyMaterials.map((material) => {
              const checked = selectedMaterials.includes(material.id);
              return (
                <li key={material.id}>
                  <label className={checked ? `${styles.material} ${styles.checked}` : styles.material}>
                    <input
                      type="checkbox"
                      className="srOnly"
                      checked={checked}
                      onChange={() => toggleMaterial(material.id)}
                    />
                    <span className={styles.materialName}>{material.filename}</span>
                    <span className={styles.materialMeta}>{material.typeLabel}</span>
                  </label>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className={styles.row}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor="practice-count">
            题目数量
          </label>
          <input
            id="practice-count"
            className={styles.input}
            type="number"
            min={1}
            max={MAX_QUESTIONS}
            value={questionCount}
            onChange={(event) => setQuestionCount(Number(event.target.value))}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor="practice-difficulty">
            难度
          </label>
          <select
            id="practice-difficulty"
            className={styles.input}
            value={difficulty}
            onChange={(event) => setDifficulty(event.target.value as PracticeDifficultyDto)}
          >
            {DIFFICULTY_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      <fieldset className={styles.fieldset}>
        <legend className={styles.label}>题型</legend>
        <div className={styles.types}>
          {TYPE_OPTIONS.map((option) => {
            const checked = types.includes(option.value);
            return (
              <label
                key={option.value}
                className={checked ? `${styles.type} ${styles.checked}` : styles.type}
              >
                <input
                  type="checkbox"
                  className="srOnly"
                  checked={checked}
                  onChange={() => toggleType(option.value)}
                />
                {option.label}
              </label>
            );
          })}
        </div>
      </fieldset>

      {problems.length > 0 ? (
        <ul className={styles.problems} role="alert">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      ) : null}

      {error ? (
        <p className={styles.error} role="alert">
          {error.message}
        </p>
      ) : null}

      <div className={styles.footer}>
        <Button
          variant="primary"
          onClick={handleSubmit}
          disabled={generate.isPending || readyMaterials.length === 0}
        >
          {generate.isPending ? "正在提交…" : "生成练习"}
        </Button>
        <span className={styles.hint}>生成由后台任务完成，提交后可以继续做别的事。</span>
      </div>
    </div>
  );
}
