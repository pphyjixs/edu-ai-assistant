/**
 * 创建课程（仅教师，契约 3）。
 *
 * `name` 去空白后 1–100 字符；`description` 最多 2000 字符，省略时后端默认为空串。
 * 创建成功后进入课程管理页——邀请码就在那里，教师下一步通常要把它发给学生。
 */

import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "@/components/Button/Button";
import { useCreateCourse } from "@/features/courses/hooks/useCourses";
import { toAppError } from "@/services/http";

import styles from "./CreateCourseForm.module.css";

const NAME_MAX = 100;
const DESCRIPTION_MAX = 2000;

export function CreateCourseForm() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const create = useCreateCourse();

  const error = create.isError ? toAppError(create.error) : null;

  function submit() {
    const trimmed = name.trim();
    if (trimmed.length === 0) {
      setNameError("请输入课程名称。");
      return;
    }
    if (trimmed.length > NAME_MAX) {
      setNameError(`课程名称最多 ${NAME_MAX} 个字符。`);
      return;
    }
    setNameError(null);

    create.mutate(
      { name: trimmed, description },
      {
        onSuccess: (course) => {
          setName("");
          setDescription("");
          // 邀请码在管理页，那是教师创建课程后最需要的地方
          navigate(`/courses/${course.id}/manage`);
        },
      },
    );
  }

  return (
    <div className={styles.form}>
      <div className={styles.field}>
        <label className={styles.label} htmlFor="course-name">
          课程名称
        </label>
        <input
          id="course-name"
          className={styles.input}
          value={name}
          maxLength={NAME_MAX}
          placeholder="例如：软件工程实验"
          onChange={(event) => setName(event.target.value)}
        />
        {nameError ? <p className={styles.fieldError}>{nameError}</p> : null}
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor="course-description">
          课程说明 <span className={styles.optional}>选填</span>
        </label>
        <textarea
          id="course-description"
          className={styles.textarea}
          rows={3}
          maxLength={DESCRIPTION_MAX}
          value={description}
          placeholder="这门课会讲什么、学生需要准备什么"
          onChange={(event) => setDescription(event.target.value)}
        />
      </div>

      {error ? (
        <p className={styles.error} role="alert">
          {error.message}
        </p>
      ) : null}

      <div>
        <Button variant="primary" onClick={submit} disabled={create.isPending}>
          {create.isPending ? "创建中…" : "创建课程"}
        </Button>
      </div>
    </div>
  );
}
